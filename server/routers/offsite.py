"""«Копия на флешку»: HTTP-слой.

Суперадмин скачивает из панели запертую паролем копию сервера и tar-архивы роликов. Состояние, сборка
копии, Redis и журнал — в offsite_store, правила и шифрование — в offsite_rules / offsite_crypto, потоковый
tar — в offsite_tar; здесь они только склеены.

Оба скачивания идут по одноразовой ссылке. Браузер качает гигабайты прямо на диск обычным переходом по
ссылке, без заголовка Authorization, поэтому «входом» служит сам токен: его выдаёт авторизованный POST,
он живёт 2 минуты и срабатывает один раз (Redis GETDEL). Права проверяются при выдаче, а не при скачивании.

«Скачано» отмечается только когда последний кусок потока отдан: оборвали вкладку на середине — ни копия,
ни «водяной знак» роликов не сдвигаются, в следующий раз те же ролики снова «новые». Отметку делает сам
генератор потока новой сессией БД: сессия запроса (get_db) закрывается раньше, чем начнётся отдача тела.

Эндпоинты, которые собирают или стримят, — обычные `def`: FastAPI гонит их в пуле потоков, и сборка копии
(дамп, шифрование, минуты работы) не блокирует остальные запросы сервера.

Пароли (входа и на копию) не попадают ни в ответы, ни в журнал, ни в логи.
"""
import hmac
import logging
import os
import re
import shutil
import stat
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

import offsite_store as store
from deps import MEDIA_PATH, SECRET_KEY, SessionLocal, get_db, require_superadmin
from offsite_crypto import open_secret, seal_secret
from offsite_rules import copy_password_problem, media_filename, reminder_state
from offsite_tar import TarItem, iter_tar, tar_size
from placements_store import now_msk

log = logging.getLogger(__name__)
router = APIRouter()

CHUNK = 1 << 20                                 # кусок потока: 1 МиБ — гигабайты в память не попадают
MEDIA_MANIFEST = "MANIFEST_media.txt"
DOWNLOAD_URL = "/api/offsite/download?t="       # снаружи сервер виден под /api/ (nginx его срезает)

MSG_BAD_LOGIN = "Неверный пароль входа"
MSG_BAD_CURRENT = "Неверный текущий пароль на копию"
MSG_NO_PASSWORD = "Сначала задайте пароль на копию"
MSG_NOT_MOUNTED = "Сервер не обновлён для копии на флешку: запустите install_server.sh"
MSG_REDIS = "Хранилище одноразовых ссылок (Redis) недоступно — повторите позже"
MSG_BAD_TICKET = "Ссылка недействительна или устарела — запросите скачивание заново"
MSG_NO_NEW_MEDIA = "Новых роликов нет"
MSG_NO_MEDIA = "В медиатеке нет роликов"

# Имя файла в заголовке Content-Disposition берётся из токена в Redis: пускаем только то, что умеют
# выдавать наши offsite_rules (латиница, цифры, точка, дефис, подчёркивание), иначе заголовок можно сломать.
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


class PasswordBody(BaseModel):
    # Все поля с умолчанием: пропущенное поле даёт понятный 403/400 нашим текстом, а не 422 с жаргоном.
    new_password: str = ""
    new_password2: str = ""
    login_password: str = ""
    current_password: Optional[str] = None


class CopyBody(BaseModel):
    login_password: str = ""


class MediaBody(BaseModel):
    scope: str = ""


# --- общие мелочи ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    """Момент как строка «ГГГГ-ММ-ДДTЧЧ:ММ:СС» (наивное МСК, без долей секунды)."""
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _int_or_none(value) -> Optional[int]:
    return int(value) if value is not None else None


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} МБ"


def _stored_password(state: dict) -> Optional[str]:
    """Пароль на копию открытым текстом; None — не задан или не читается (сменился SECRET_KEY)."""
    enc = state["password_enc"]
    if not enc:
        return None
    return open_secret(enc, SECRET_KEY)


def _same_secret(given: Optional[str], expected: str) -> bool:
    """Сравнение без утечки по времени (compare_digest не принимает не-ASCII строки — сравниваем байты)."""
    return hmac.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))


def _refuse_login(db, who: str, title: str, reason: str, message: str):
    """Отказ 403 с записью в журнал. Запись коммитим до raise: сессия запроса при ошибке откатывается,
    а попытку подбора пароля надо видеть. В журнале только причина словами — никаких паролей."""
    store.audit(db, title, reason, who)
    db.commit()
    raise HTTPException(403, message)


def _issue_ticket(payload: dict) -> str:
    """Выдать одноразовую ссылку; Redis недоступен — 503, а не 500 с трассировкой."""
    try:
        return store.issue_ticket(payload)
    except (RedisError, OSError):
        raise HTTPException(503, MSG_REDIS)


def _remove_workdir(workdir: str) -> None:
    """Стереть папку сборки копии — но только настоящую папку прямо внутри OFFSITE_TMP.
    Путь приходит из токена в Redis; чужую папку по нему мы не трогаем."""
    if _own_workdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)


def _own_workdir(workdir) -> bool:
    if not isinstance(workdir, str) or not workdir or os.path.islink(workdir):
        return False
    real = os.path.realpath(workdir)
    return os.path.dirname(real) == os.path.realpath(store.OFFSITE_TMP) and os.path.isdir(real)


def _finish(mark) -> None:
    """Отметить «скачано» новой сессией и закоммитить. Зовётся только после последнего куска потока.

    Не получилось (база недоступна) — файл у человека уже целый, ломать ему ответ незачем: пишем в лог
    и оставляем напоминание «warn» (честнее лишний раз напомнить, чем отметить то, чего не записали).
    """
    try:
        db = SessionLocal()
        try:
            mark(db)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.error("Копия на флешку: не удалось отметить скачивание (%s: %s)", type(e).__name__, str(e)[:200])


# --- статус ---------------------------------------------------------------------------------

@router.get("/offsite/status")
def offsite_status(admin: dict = Depends(require_superadmin), db: Session = Depends(get_db)):
    now = now_msk()
    st = store.load_state(db)
    totals = store.media_totals(db, st["media_watermark"])
    plain = _stored_password(st)
    last_copy = st["last_copy_at"]
    return {
        "password": {
            "set": plain is not None,
            "set_at": _iso(st["password_set_at"]),
            "set_by": st["password_set_by"],
            # пароль в базе есть, но открыть его нечем (SECRET_KEY сменили): панель просит задать заново
            "needs_reset": bool(st["password_enc"]) and plain is None,
        },
        "copy": {
            "last_at": _iso(last_copy),
            "last_by": st["last_copy_by"],
            "last_file": st["last_copy_file"],
            "last_bytes": _int_or_none(st["last_copy_bytes"]),
            "today": last_copy is not None and last_copy.date() == now.date(),
        },
        "media": {
            "new_count": totals["new_count"], "new_bytes": totals["new_bytes"],
            "all_count": totals["all_count"], "all_bytes": totals["all_bytes"],
            "last_at": _iso(st["last_media_at"]), "last_by": st["last_media_by"],
        },
        "state": reminder_state(last_copy, totals["new_count"], now),
        "project_mounted": store.project_mounted(),
    }


# --- пароль на копию ------------------------------------------------------------------------

@router.post("/offsite/password")
def offsite_set_password(body: PasswordBody, admin: dict = Depends(require_superadmin),
                         db: Session = Depends(get_db)):
    """Задать или сменить пароль на копию.

    Порядок проверок важен: сначала пароль входа (токен без него ничего не узнает), потом текущий пароль
    на копию (иначе тот, кто сел за открытую панель, подсунул бы свой пароль и унёс копию, которую
    сумеет открыть), и только потом правила нового пароля.
    """
    who = admin["username"]
    if not store.check_login_password(db, who, body.login_password):
        _refuse_login(db, who, "Неверный пароль при смене пароля на копию", "пароль входа не совпал", MSG_BAD_LOGIN)
    current = _stored_password(store.load_state(db))
    # Текущий пароль требуем, только если он есть и читается: если SECRET_KEY сменили, проверять нечем.
    if current is not None and not _same_secret(body.current_password, current):
        _refuse_login(db, who, "Неверный пароль при смене пароля на копию",
                      "текущий пароль на копию не совпал", MSG_BAD_CURRENT)
    if body.new_password != body.new_password2:
        raise HTTPException(400, "Пароли не совпадают")
    problem = copy_password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    store.save_password(db, seal_secret(body.new_password, SECRET_KEY), who, now_msk())
    store.audit(db, "Пароль на копию изменён" if current is not None else "Пароль на копию задан", "", who)
    db.commit()
    return {"status": "saved"}


# --- ежедневная копия -----------------------------------------------------------------------

@router.post("/offsite/copy")
def offsite_make_copy(body: CopyBody, request: Request, admin: dict = Depends(require_superadmin),
                      db: Session = Depends(get_db)):
    """Собрать копию сервера и выдать одноразовую ссылку на неё. Долгий запрос (минуты на большой базе)."""
    who = admin["username"]
    if not store.check_login_password(db, who, body.login_password):
        _refuse_login(db, who, "Неверный пароль при скачивании копии", "пароль входа не совпал", MSG_BAD_LOGIN)
    password = _stored_password(store.load_state(db))
    if password is None:
        raise HTTPException(409, MSG_NO_PASSWORD)

    store.cleanup_stale()                       # забытые копии прошлых запросов: не копим на диске
    try:
        built = store.build_copy(db, who=who, host=request.headers.get("host"),
                                 password=password, now=now_msk())
    except store.ProjectNotMounted:
        raise HTTPException(503, MSG_NOT_MOUNTED)
    except (RuntimeError, ValueError, OSError) as e:
        # build_copy уже убрал за собой папку; текст ошибки очищен от пароля БД и пароля на копию
        raise HTTPException(500, str(e)[:500] or type(e).__name__)
    try:
        token = _issue_ticket({"kind": "copy", "workdir": built.workdir, "filename": built.filename,
                               "who": who})
    except HTTPException:
        _remove_workdir(built.workdir)          # ссылки нет — собранная копия никому не нужна, а на диске опасна
        raise
    return {"url": DOWNLOAD_URL + token, "filename": built.filename, "bytes": built.size}


# --- ролики ---------------------------------------------------------------------------------

@router.post("/offsite/media")
def offsite_make_media(body: MediaBody, admin: dict = Depends(require_superadmin),
                       db: Session = Depends(get_db)):
    """Выдать ссылку на tar с роликами: «new» — всё, что новее скачанного раньше, «all» — вся медиатека.

    Верхнюю границу upto_id запоминаем сейчас: ролик, загруженный, пока идёт скачивание, в архив не
    попадёт и потому не должен стать «уже скачанным».
    """
    if body.scope not in ("new", "all"):
        raise HTTPException(400, "Объём роликов: new (новые) или all (все)")
    who = admin["username"]
    watermark = store.load_state(db)["media_watermark"]
    totals = store.media_totals(db, watermark)
    after = watermark if body.scope == "new" else 0
    count = totals["new_count"] if body.scope == "new" else totals["all_count"]
    size = totals["new_bytes"] if body.scope == "new" else totals["all_bytes"]
    upto = totals["max_id"]
    if count == 0 or upto <= after:
        raise HTTPException(409, MSG_NO_NEW_MEDIA if body.scope == "new" else MSG_NO_MEDIA)
    filename = media_filename(body.scope, now_msk(), after, upto)
    token = _issue_ticket({"kind": "media", "scope": body.scope, "after": after, "upto": upto,
                           "filename": filename, "who": who})
    return {"url": DOWNLOAD_URL + token, "filename": filename, "count": count, "bytes": size}


# --- скачивание по одноразовой ссылке -------------------------------------------------------

@router.get("/offsite/download")
def download(t: str = "", db: Session = Depends(get_db)):
    """Отдать то, на что выдана ссылка. Входа нет: токен и есть право (см. модуль)."""
    try:
        payload = store.take_ticket(t)
    except (RedisError, OSError):
        raise HTTPException(503, MSG_REDIS)
    if payload is None:
        raise HTTPException(404, MSG_BAD_TICKET)
    filename = payload.get("filename")
    who = payload.get("who")
    if (not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename)
            or not isinstance(who, str) or not who):
        raise HTTPException(404, MSG_BAD_TICKET)
    if payload.get("kind") == "copy":
        return _copy_response(payload.get("workdir"), filename, who)
    if payload.get("kind") == "media":
        return _media_response(db, payload, filename, who)
    raise HTTPException(404, MSG_BAD_TICKET)


def _headers(filename: str, size: int) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size), "Cache-Control": "no-store"}


def _copy_response(workdir, filename: str, who: str):
    if not _own_workdir(workdir):
        raise HTTPException(404, MSG_BAD_TICKET)
    try:
        # Открываем сейчас, а не в генераторе: открытый файл переживёт стирание папки чисткой забытых
        # копий (соседний запрос), и скачивание, которое уже пошло, не оборвётся.
        f = open(os.path.join(workdir, filename), "rb")
    except OSError:
        _remove_workdir(workdir)
        raise HTTPException(404, MSG_BAD_TICKET)
    size = os.fstat(f.fileno()).st_size
    return StreamingResponse(
        _stream_copy(f, size, workdir, filename, who), media_type="application/x-tar",
        headers=_headers(filename, size),
        # Если ответ так и не начали читать, finally генератора не сработает — подстрахуемся (идемпотентно)
        background=BackgroundTask(_drop_copy, f, workdir))


def _drop_copy(f, workdir: str) -> None:
    try:
        f.close()
    finally:
        _remove_workdir(workdir)


def _stream_copy(f, size: int, workdir: str, filename: str, who: str):
    try:
        left = size
        while left:
            buf = f.read(min(CHUNK, left))
            if not buf:
                raise RuntimeError("Файл копии на сервере стал короче заявленного")
            left -= len(buf)
            yield buf
        # Сюда доходим, только если ушёл последний кусок: обрыв (GeneratorExit) или ошибка — мимо.
        _finish(lambda db: _mark_copy(db, who, filename, size))
    finally:
        _drop_copy(f, workdir)                  # и при обрыве, и при ошибке: расшифрованная копия не остаётся


def _mark_copy(db, who: str, filename: str, size: int) -> None:
    store.mark_copy_done(db, who, filename, size, now_msk())
    store.audit(db, "Копия на флешку скачана", f"{filename}, {_mb(size)}", who)


def _cell(value) -> str:
    """Значение в одну ячейку манифеста: табы и переводы строк внутри названия ломали бы столбцы."""
    return " ".join(str(value if value is not None else "").split())


def _media_response(db, payload: dict, filename: str, who: str):
    scope, after, upto = payload.get("scope"), payload.get("after"), payload.get("upto")
    if (scope not in ("new", "all") or not isinstance(after, int) or not isinstance(upto, int)
            or isinstance(after, bool) or isinstance(upto, bool) or after < 0 or upto <= after):
        raise HTTPException(404, MSG_BAD_TICKET)
    # Строки читаем заново, а не из токена: за минуты между выдачей и скачиванием файлы могли пропасть,
    # а размер в Content-Length должен соответствовать тому, что реально пойдёт в поток.
    files, lines, total = _media_files(store.media_rows(db, after, upto))
    manifest = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    items = [TarItem("media/" + MEDIA_MANIFEST, len(manifest), data=manifest, mtime=int(time.time()))] + files
    size = tar_size(items)
    return StreamingResponse(
        _stream_media(items, who, scope, after, upto, len(files), total), media_type="application/x-tar",
        headers=_headers(filename, size))


def _media_files(rows) -> tuple[list, list, int]:
    """Файлы роликов для tar, строки манифеста и сумма байт.

    Берём только обычные файлы, лежащие в MEDIA_PATH под своим именем (basename: имя в базе — данные
    пользователя, «../x» не должно вывести за раздел). Нет файла — строка остаётся в манифесте с пометкой
    «нет файла»: потерянное видно, а архив всё равно собирается. Один файл на две строки базы кладём раз.
    """
    files, lines, seen, total = [], [], set(), 0
    for r in rows:
        base = store.safe_basename(r.filename)
        st = None
        # файл с именем манифеста затёр бы манифест в архиве — считаем его отсутствующим
        if base is not None and base != MEDIA_MANIFEST:
            try:
                st = os.lstat(os.path.join(MEDIA_PATH, base))
            except OSError:
                st = None
        head = f"{r.id}\t{_cell(r.title)}\t{_cell(base or r.filename)}\t"
        tail = f"\t{_cell(r.md5_hash)}"
        if st is None or not stat.S_ISREG(st.st_mode):
            lines.append(head + str(int(r.filesize or 0)) + tail + "\tнет файла")
            continue
        lines.append(head + str(st.st_size) + tail)
        if base in seen:
            continue
        seen.add(base)
        files.append(TarItem("media/" + base, st.st_size, path=os.path.join(MEDIA_PATH, base),
                             mtime=int(st.st_mtime)))
        total += st.st_size
    return files, lines, total


def _stream_media(items, who: str, scope: str, after: int, upto: int, count: int, total: int):
    yield from iter_tar(items, CHUNK)
    # Весь архив ушёл — только теперь водяной знак двигается до upto (а не до текущего максимума)
    _finish(lambda db: _mark_media(db, who, scope, after, upto, count, total))


def _mark_media(db, who: str, scope: str, after: int, upto: int, count: int, total: int) -> None:
    store.mark_media_done(db, who, scope, upto, count, total, now_msk())
    kind = "новые" if scope == "new" else "все"
    store.audit(db, "Ролики скачаны на флешку",
                f"{kind}, {count} шт, {_mb(total)}, id {after + 1}–{upto}", who)
