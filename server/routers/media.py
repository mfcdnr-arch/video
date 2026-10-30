"""Медиабиблиотека, рекламодатели/папки, файлы для мини ПК."""
import hashlib
import logging
import os
import subprocess
import time
import uuid
from datetime import timedelta
from typing import Optional

import aiofiles
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from broadcast_store import get_broadcast
from deps import engine, get_db, get_current_admin, require_write, require_moderator, verify_device_token, MEDIA_PATH
from placements_logic import playlists_for_screen
from placements_store import load_overrides, load_placements, now_msk
from playable_store import DEFAULT_VENUE, venue_sql

log = logging.getLogger(__name__)
router = APIRouter()

THUMBS_DIR = os.path.join(MEDIA_PATH, ".thumbs")
os.makedirs(THUMBS_DIR, exist_ok=True)


# ─── 38-ФЗ «О рекламе»: категории и правила ─────────────────────────────────
# Категория декларируется при загрузке. Правила по категориям:
#   blocked    — реклама запрещена законом полностью, загрузка отклоняется;
#   disclaimer — обязательный текст предупреждения (показывается в ролике,
#                здесь фиксируется его наличие и формулировка);
#   license    — обязателен номер лицензии (финансовые услуги, ст. 28);
#   min_age    — принудительная возрастная маркировка не ниже указанной;
#   auto_ok    — не реклама (служебный контент/заглушки) — без модерации.
AD_CATEGORIES = {
    "service":   {"label": "Служебный контент (не реклама)", "auto_ok": True},
    "food":      {"label": "Продукты питания"},
    "children":  {"label": "Детские товары"},
    "other":     {"label": "Прочие товары и услуги"},
    "medical":   {"label": "Лекарства, медицина (ст. 24)",
                  "disclaimer": True,
                  "disclaimer_hint": "Имеются противопоказания. Необходима консультация специалиста."},
    "financial": {"label": "Финансовые услуги (ст. 28)",
                  "disclaimer": True, "license": True,
                  "disclaimer_hint": "Подробные условия — на сайте/в офисе организации."},
    "alcohol":   {"label": "Алкоголь — только в местах продаж (ст. 21)",
                  "disclaimer": True, "min_age": "18+",
                  "disclaimer_hint": "Чрезмерное употребление алкоголя вредит вашему здоровью."},
    "gambling":  {"label": "Азартные игры, букмекеры (ст. 27)",
                  "disclaimer": True, "min_age": "18+",
                  "disclaimer_hint": "Участие в азартных играх может вызвать зависимость."},
    "tobacco":   {"label": "Табак, никотин — реклама ЗАПРЕЩЕНА (ст. 7)", "blocked": True},
}
AGE_RATINGS = ("0+", "6+", "12+", "16+", "18+")


def _validate_ad_fields(category, age_rating, disclaimer_text, license_number):
    """Проверка декларации по 38-ФЗ. Возвращает (category, age_rating) или бросает 400."""
    if category not in AD_CATEGORIES:
        raise HTTPException(400, f"category: допустимые значения — {', '.join(AD_CATEGORIES)}")
    rules = AD_CATEGORIES[category]

    if rules.get("blocked"):
        raise HTTPException(400,
            "Реклама табачной и никотинсодержащей продукции запрещена "
            "(ст. 7 закона «О рекламе», 38-ФЗ) — загрузка отклонена")

    if rules.get("auto_ok"):
        return category, age_rating or None

    if not age_rating:
        raise HTTPException(400, "Для рекламы обязательна возрастная маркировка (age_rating: 0+/6+/12+/16+/18+)")
    if age_rating not in AGE_RATINGS:
        raise HTTPException(400, f"age_rating: допустимые значения — {', '.join(AGE_RATINGS)}")
    min_age = rules.get("min_age")
    if min_age and AGE_RATINGS.index(age_rating) < AGE_RATINGS.index(min_age):
        raise HTTPException(400, f"Для категории «{rules['label']}» маркировка не может быть ниже {min_age}")

    if rules.get("disclaimer") and not (disclaimer_text or "").strip():
        hint = rules.get("disclaimer_hint", "")
        raise HTTPException(400,
            f"Для категории «{rules['label']}» обязателен текст предупреждения "
            f"(disclaimer_text). Пример: «{hint}»")

    if rules.get("license") and not (license_number or "").strip():
        raise HTTPException(400,
            f"Для категории «{rules['label']}» обязателен номер лицензии (license_number)")

    return category, age_rating


def _make_thumbnail(src_path: str, media_id: int) -> Optional[str]:
    """Сгенерировать миниатюру 320px (jpg) через ffmpeg. Возвращает путь или None.
    Для видео берётся представительный кадр (фильтр thumbnail), для картинок —
    само изображение в уменьшенном виде."""
    thumb_path = os.path.join(THUMBS_DIR, f"{media_id}.jpg")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path,
             "-vf", "thumbnail,scale=320:-2", "-frames:v", "1",
             thumb_path],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(thumb_path):
            return thumb_path
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


# ─── Дубли: один и тот же файл (по md5) загружен повторно ───────────────────

DUPLICATE_MESSAGE = "Такой ролик уже есть в медиатеке"


def group_duplicates(rows) -> dict:
    """Дубли среди роликов: для каждого id — список ОСТАЛЬНЫХ роликов с тем же
    md5_hash ({"id", "title"}, в порядке rows). Только группы из двух и более;
    пустой md5_hash (None, "", пробелы) не считается совпадением. Чистая
    функция: rows — строки с полями id, title, md5_hash."""
    groups = {}
    for r in rows:
        h = (r.md5_hash or "").strip()
        if h:
            groups.setdefault(h, []).append(r)
    result = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        for r in members:
            result[r.id] = [{"id": o.id, "title": o.title} for o in members if o.id != r.id]
    return result


def _duplicate_detail(row) -> dict:
    """Тело ответа 409 «такой ролик уже есть»: сообщение + сведения о ролике
    из медиатеки (row — результат _find_duplicate). Чистая функция. Дата
    переводится в строку заранее: HTTPException(detail=...) уходит в
    JSONResponse без jsonable_encoder, и datetime там упал бы с TypeError."""
    created = row.created_at
    return {"message": DUPLICATE_MESSAGE,
            "duplicate_of": {"id": row.id, "title": row.title, "filename": row.filename,
                             "folder": row.folder, "advertiser": row.advertiser,
                             "created_at": created.isoformat() if created is not None else None,
                             "duration_seconds": row.duration_seconds,
                             "playlists": int(row.playlists or 0)}}


def _find_duplicate(md5_hash: str):
    """Самый старый не-документ с таким md5 (или None) и сведения о нём для
    сообщения: папка, рекламодатель, число плейлистов, где он стоит."""
    with Session(engine) as db:
        return db.execute(text("""
            SELECT m.id, m.title, m.filename, f.name AS folder, a.name AS advertiser,
                   m.created_at, m.duration_seconds,
                   (SELECT COUNT(DISTINCT pi.playlist_id) FROM playlist_items pi
                     WHERE pi.media_id = m.id) AS playlists
            FROM media m
            LEFT JOIN media_folders f ON f.id = m.folder_id
            LEFT JOIN advertisers a ON a.id = m.advertiser_id
            WHERE m.md5_hash = :h AND m.status <> 'document'
            ORDER BY m.created_at, m.id
            LIMIT 1
        """), {"h": md5_hash}).fetchone()


# ─── Медиабиблиотека ────────────────────────────────────────────────────────

@router.post("/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    title: str = Query(...),
    valid_from: Optional[str] = Query(None, description="Показывать с (ISO datetime)"),
    valid_until: Optional[str] = Query(None, description="Показывать по (ISO datetime)"),
    advertiser_id: Optional[int] = Query(None),
    folder_id: Optional[int] = Query(None),
    image_seconds: Optional[int] = Query(None, ge=1, le=300,
        description="Длительность показа баннера-картинки, сек (для видео игнорируется)"),
    category: str = Query("other", description="Категория по 38-ФЗ"),
    age_rating: Optional[str] = Query(None, description="Возрастная маркировка 0+/6+/12+/16+/18+"),
    disclaimer_text: Optional[str] = Query(None, description="Текст обязательного предупреждения"),
    license_number: Optional[str] = Query(None, description="№ лицензии (финансовые услуги)"),
    is_filler: bool = Query(False, description="Загрузить как заглушку (папка «Заглушки»)"),
    allow_duplicate: bool = Query(False,
        description="Загрузить, даже если такой файл (по md5) уже есть в медиатеке"),
    admin: dict = Depends(require_write)
):
    """Загрузить видеофайл на сервер. Категория по 38-ФЗ обязательна;
    табак блокируется; регулируемые категории требуют доп. полей.
    Новый файл попадает на модерацию (review_status='pending') и не
    выдаётся экранам до одобрения; служебный контент — сразу approved.
    is_filler=true — файл сразу в «Заглушки»: категория service (не реклама,
    модерация не нужна), рекламодатель/папка «Служебное»/«Заглушки».
    Если ролик с тем же md5 уже есть, загрузка отклоняется кодом 409 (в detail —
    сведения о нём); allow_duplicate=true загружает файл всё равно. На документы
    (папка «Документы») проверка не распространяется."""
    # Куда грузим, выясняем ДО проверок 38-ФЗ: скан договора — не реклама,
    # требовать у него возрастную маркировку и категорию бессмысленно.
    is_document = False
    if folder_id:
        with Session(engine) as db_chk:
            fname = db_chk.execute(text("SELECT name FROM media_folders WHERE id = :id"),
                                   {"id": folder_id}).scalar()
        is_document = (fname or "").strip().lower() == DOCS_FOLDER.lower()

    if is_document:
        category, age_rating, review_status = "service", None, "approved"
    else:
        if is_filler:
            category = "service"
        category, age_rating = _validate_ad_fields(category, age_rating, disclaimer_text, license_number)
        review_status = "approved" if AD_CATEGORIES[category].get("auto_ok") else "pending"
    # Случайный суффикс защищает от коллизии имён: два файла с одинаковым
    # исходным именем, загруженные в одну секунду, раньше перезаписывали
    # друг друга на диске (обнаружено при тестировании модерации).
    filename = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{file.filename.replace(' ', '_')}"
    filepath = os.path.join(MEDIA_PATH, filename)

    md5 = hashlib.md5()
    size = 0
    async with aiofiles.open(filepath, 'wb') as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            await f.write(chunk)
            md5.update(chunk)
            size += len(chunk)

    md5_hash = md5.hexdigest()

    # Тот же файл уже есть в медиатеке — предупреждаем (мягко: allow_duplicate
    # пропускает). Файл уже записан на диск, поэтому при отказе его удаляем:
    # до проверки плеером, миниатюры и INSERT ничего ещё не создано.
    if not is_document and not allow_duplicate:
        dup = _find_duplicate(md5_hash)
        if dup:
            try:
                os.remove(filepath)
            except OSError:
                pass
            raise HTTPException(409, detail=_duplicate_detail(dup))

    # Файл из папки «Документы» — это не эфирный контент, а скан договора,
    # декларации или согласования. Такие файлы:
    #   • не проходят проверку плеером (PDF ffprobe не откроет);
    #   • получают status='document' — а весь эфир строго фильтруется по
    #     status='ready', поэтому в плейлист и на экран они не попадут никак.
    if is_document:
        if not filename.lower().endswith(DOC_EXTS):
            try:
                os.remove(filepath)
            except OSError:
                pass
            raise HTTPException(400, "В папку «Документы» принимаются файлы "
                                     + ", ".join(DOC_EXTS))
        with Session(engine) as db:
            media_id = db.execute(text(
                "INSERT INTO media (title, filename, filesize, md5_hash, status, "
                "duration_seconds, advertiser_id, folder_id, category, review_status) "
                "VALUES (:t, :f, :s, :m, 'document', 0, :aid, :fid, 'service', 'approved') "
                "RETURNING id"
            ), {"t": title, "f": filename, "s": size, "m": md5_hash,
                "aid": advertiser_id or None, "fid": folder_id}).fetchone()[0]
            db.commit()
        return {"id": media_id, "title": title, "filename": filename, "filesize": size,
                "md5_hash": md5_hash, "status": "document", "duration_seconds": 0,
                "category": "service", "age_rating": None, "review_status": "approved",
                "note": "Документ сохранён в папке «Документы» и в эфир не выдаётся"}

    # Проверка файла на работоспособность (отсекаем заведомо нерабочие).
    from media_check import check_video
    check = check_video(filepath)
    if not check["ok"]:
        # удаляем непригодный файл, не сохраняем в медиатеку
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"Файл не прошёл проверку: {check.get('error') or 'неизвестная ошибка'}")

    duration = check.get("duration", 0) or 0
    # Индивидуальная длительность баннера: перекрывает стандартные 10 секунд.
    # Агент показывает картинку ровно duration_seconds (см. image_durations
    # в ответе /schedule/minipc), биллинг считает те же секунды.
    if check.get("is_image") and image_seconds:
        duration = float(image_seconds)

    with Session(engine) as db:
        if is_filler and not advertiser_id:
            # Заглушки складываются в служебную папку (создана миграцией 009;
            # на всякий случай добиваем, если её удалили руками)
            db.execute(text("INSERT INTO advertisers (name, color, note) VALUES "
                            "('Служебное', '#9298a3', 'Системные материалы: заглушки') "
                            "ON CONFLICT (name) DO NOTHING"))
            row = db.execute(text("SELECT id FROM advertisers WHERE name='Служебное'")).fetchone()
            advertiser_id = row[0]
            db.execute(text("INSERT INTO media_folders (advertiser_id, name) "
                            "SELECT :a, 'Заглушки' WHERE NOT EXISTS "
                            "(SELECT 1 FROM media_folders WHERE advertiser_id=:a AND name='Заглушки')"),
                       {"a": advertiser_id})
            frow = db.execute(text("SELECT id FROM media_folders WHERE advertiser_id=:a AND name='Заглушки'"),
                              {"a": advertiser_id}).fetchone()
            folder_id = frow[0]
        result = db.execute(text(
            "INSERT INTO media (title, filename, filesize, md5_hash, status, duration_seconds, "
            "valid_from, valid_until, advertiser_id, folder_id, is_filler, "
            "category, age_rating, disclaimer_text, license_number, review_status) "
            "VALUES (:t, :f, :s, :m, 'ready', :dur, :vf, :vu, :aid, :fid, :fil, "
            ":cat, :age, :disc, :lic, :rev) RETURNING id"
        ), {"t": title, "f": filename, "s": size, "m": md5_hash, "dur": duration,
            "vf": valid_from or None, "vu": valid_until or None,
            "aid": advertiser_id or None, "fid": folder_id or None,
            "fil": bool(is_filler),
            "cat": category, "age": age_rating,
            "disc": (disclaimer_text or "").strip() or None,
            "lic": (license_number or "").strip() or None,
            "rev": review_status})
        media_id = result.fetchone()[0]
        db.commit()

    _make_thumbnail(filepath, media_id)

    return {"id": media_id, "title": title, "filename": filename,
            "filesize": size, "md5_hash": md5_hash, "status": "ready",
            "duration_seconds": duration,
            "category": category, "age_rating": age_rating,
            "review_status": review_status,
            "warning": check.get("warning"),
            "codec": check.get("codec"),
            "resolution": (f"{check.get('width')}x{check.get('height')}"
                           if check.get("width") else None)}


@router.get("/media")
def list_media(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text(
        "SELECT m.id, m.title, m.filename, m.filesize, m.md5_hash, m.duration_seconds, "
        "m.status, m.created_at, COALESCE(m.is_filler, FALSE) AS is_filler, "
        "m.review_status, m.advertiser_id, m.folder_id, a.name AS advertiser "
        "FROM media m LEFT JOIN advertisers a ON a.id = m.advertiser_id "
        # Это список ЭФИРНОГО контента (из него же выбирают ролик в плейлист),
        # поэтому сканы из папки «Документы» сюда не попадают. Их видно в самой
        # папке — /advertisers/{id}/media.
        "WHERE m.status <> 'document' "
        "ORDER BY m.created_at DESC"
    )).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/media/search")
def search_media(q: str = Query(..., min_length=2, max_length=100),
                 admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Поиск по медиатеке: название ролика, имя файла, рекламодатель."""
    rows = db.execute(text("""
        SELECT m.id, m.title, m.filename, m.duration_seconds, m.review_status,
               m.category, m.age_rating, COALESCE(m.is_filler, FALSE) AS is_filler,
               a.id AS advertiser_id, a.name AS advertiser
        FROM media m
        LEFT JOIN advertisers a ON a.id = m.advertiser_id
        WHERE m.status = 'ready' AND (
              m.title ILIKE :pat OR m.filename ILIKE :pat OR a.name ILIKE :pat)
        ORDER BY m.created_at DESC
        LIMIT 50
    """), {"pat": f"%{q}%"}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/media/duplicates")
def media_duplicates(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Дубли по всей медиатеке (кроме документов): {"<id>": [{"id","title"}, ...]} —
    для каждого ролика, у которого есть копии, список остальных копий.
    Объявлен до /media/{media_id}, иначе «duplicates» разбирался бы как id."""
    rows = db.execute(text(
        "SELECT id, title, md5_hash FROM media WHERE status <> 'document' ORDER BY id"
    )).fetchall()
    return group_duplicates(rows)


@router.get("/media/fillers")
def list_fillers(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Папка «Заглушки»: все ролики-заглушки с длительностями и итогом."""
    rows = db.execute(text("""
        SELECT id, title, filename, filesize, duration_seconds, status,
               review_status, created_at
        FROM media WHERE COALESCE(is_filler, FALSE) = TRUE
        ORDER BY created_at DESC
    """)).fetchall()
    items = [dict(r._mapping) for r in rows]
    ready = [i for i in items if i["status"] == "ready" and i["review_status"] == "approved"]
    return {"items": items,
            "count": len(items),
            "ready_count": len(ready),
            "total_seconds": round(sum(i["duration_seconds"] or 0 for i in ready), 1)}


@router.get("/media/common")
def common_media(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Общая медиатека: файлы без рекламодателя (кроме заглушек) и общие папки
    (media_folders.advertiser_id IS NULL)."""
    folders = db.execute(text("""
        SELECT f.id, f.name, COUNT(m.id) AS files
        FROM media_folders f
        LEFT JOIN media m ON m.folder_id = f.id
        WHERE f.advertiser_id IS NULL
        GROUP BY f.id ORDER BY f.name
    """)).fetchall()
    files = db.execute(text("""
        SELECT m.id, m.title, m.filename, m.filesize, m.duration_seconds,
               COALESCE(m.is_filler, FALSE) AS is_filler, m.folder_id,
               m.category, m.age_rating, m.review_status, m.reject_reason,
               EXISTS(SELECT 1 FROM playlist_items pi WHERE pi.media_id = m.id) AS in_playlist
        FROM media m
        WHERE m.advertiser_id IS NULL AND COALESCE(m.is_filler, FALSE) = FALSE
          AND m.status = 'ready'
        ORDER BY m.created_at DESC
    """)).fetchall()
    return {"folders": [dict(r._mapping) for r in folders],
            "files": [dict(r._mapping) for r in files]}


@router.post("/media/folders")
def create_common_folder(body: dict = Body(...),
                         admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    """Создать общую папку (без привязки к рекламодателю)."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name обязателен")
    # UNIQUE(advertiser_id, name) не ловит дубли при NULL — проверяем сами
    dup = db.execute(text(
        "SELECT 1 FROM media_folders WHERE advertiser_id IS NULL AND name = :n"),
        {"n": name}).fetchone()
    if dup:
        raise HTTPException(409, "Папка с таким именем уже существует")
    row = db.execute(text(
        "INSERT INTO media_folders (advertiser_id, name) VALUES (NULL, :n) RETURNING id, name"
    ), {"n": name}).fetchone()
    db.commit()
    return dict(row._mapping)


@router.get("/media/folders-all")
def list_all_folders(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Все папки медиатеки (общие и рекламодателей) — для выбора папки
    при наполнении плейлиста и переносе файлов."""
    rows = db.execute(text("""
        SELECT f.id, f.name, f.advertiser_id, a.name AS advertiser,
               COUNT(m.id) AS files
        FROM media_folders f
        LEFT JOIN advertisers a ON a.id = f.advertiser_id
        LEFT JOIN media m ON m.folder_id = f.id
        GROUP BY f.id, a.name
        ORDER BY a.name NULLS FIRST, f.name
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


@router.patch("/media/{media_id}/folder")
def move_media_to_folder(media_id: int, body: dict = Body(...),
                         admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    """Перенести файл в папку (folder_id=null — «без папки»). Рекламодатель
    файла подстраивается под владельца папки."""
    if not db.execute(text("SELECT 1 FROM media WHERE id=:id"), {"id": media_id}).fetchone():
        raise HTTPException(404, "Ролик не найден")
    folder_id = body.get("folder_id")
    adv_id = None
    if folder_id is not None:
        f = db.execute(text("SELECT id, advertiser_id FROM media_folders WHERE id=:id"),
                       {"id": int(folder_id)}).fetchone()
        if not f:
            raise HTTPException(404, "Папка не найдена")
        adv_id = f.advertiser_id
        db.execute(text("UPDATE media SET folder_id=:f, advertiser_id=:a WHERE id=:id"),
                   {"f": f.id, "a": adv_id, "id": media_id})
    else:
        db.execute(text("UPDATE media SET folder_id=NULL WHERE id=:id"), {"id": media_id})
    db.commit()
    return {"status": "ok", "folder_id": folder_id, "advertiser_id": adv_id}


@router.get("/media/{media_id}")
def get_media(media_id: int, admin: dict = Depends(get_current_admin),
              db: Session = Depends(get_db)):
    # Сведения для окна просмотра ролика: где лежит, чей он и в скольких плейлистах.
    row = db.execute(text("""
        SELECT m.*, f.name AS folder_name, a.name AS advertiser_name,
               (SELECT COUNT(DISTINCT pi.playlist_id) FROM playlist_items pi
                 WHERE pi.media_id = m.id) AS playlists_count
        FROM media m
        LEFT JOIN media_folders f ON f.id = m.folder_id
        LEFT JOIN advertisers a ON a.id = m.advertiser_id
        WHERE m.id = :id
    """), {"id": media_id}).fetchone()
    if not row:
        raise HTTPException(404, "Файл не найден")
    return dict(row._mapping)


@router.delete("/media/{media_id}")
def delete_media(media_id: int, admin: dict = Depends(require_write),
                 db: Session = Depends(get_db)):
    row = db.execute(text("SELECT filename FROM media WHERE id = :id"), {"id": media_id}).fetchone()
    if not row:
        raise HTTPException(404, "Файл не найден")
    filepath = os.path.join(MEDIA_PATH, row.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    thumb = os.path.join(THUMBS_DIR, f"{media_id}.jpg")
    if os.path.exists(thumb):
        os.remove(thumb)
    db.execute(text("DELETE FROM media WHERE id = :id"), {"id": media_id})
    db.commit()
    return {"status": "deleted"}


# ─── Модерация (38-ФЗ, Tier 2) ──────────────────────────────────────────────

@router.get("/moderation/categories")
def moderation_categories(admin: dict = Depends(get_current_admin)):
    """Справочник категорий с правилами — для формы загрузки в панели."""
    return [{"key": k, **v} for k, v in AD_CATEGORIES.items()]


@router.get("/moderation/pending")
def moderation_pending(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT m.id, m.title, m.filename, m.duration_seconds, m.created_at,
               m.category, m.age_rating, m.disclaimer_text, m.license_number,
               a.name AS advertiser
        FROM media m
        LEFT JOIN advertisers a ON a.id = m.advertiser_id
        WHERE m.review_status = 'pending' AND m.status = 'ready'
        ORDER BY m.created_at
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


def _get_pending(db, media_id):
    row = db.execute(text(
        "SELECT id, title, review_status FROM media WHERE id = :id"), {"id": media_id}).fetchone()
    if not row:
        raise HTTPException(404, "Файл не найден")
    return row


@router.post("/media/{media_id}/approve")
def approve_media(media_id: int, admin: dict = Depends(require_moderator),
                  db: Session = Depends(get_db)):
    """Одобрить ролик к показу. Фиксируется кто/когда — доказательная база."""
    row = _get_pending(db, media_id)
    who = admin.get("username", "admin")
    db.execute(text("""
        UPDATE media SET review_status = 'approved', reviewed_by = :who,
               reviewed_at = NOW(), reject_reason = NULL
        WHERE id = :id
    """), {"who": who, "id": media_id})
    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor)
            VALUES ('moderation', :t, :d, :a)
        """), {"t": f"Ролик одобрен: «{row.title}»", "d": f"media_id={media_id}", "a": who})
    except Exception:
        pass
    db.commit()
    return {"status": "approved", "id": media_id, "reviewed_by": who}


@router.post("/media/{media_id}/reject")
def reject_media(media_id: int, body: dict = Body(...),
                 admin: dict = Depends(require_moderator), db: Session = Depends(get_db)):
    """Отклонить ролик (с причиной). Отклонённый контент не выдаётся экранам."""
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "Укажите причину отклонения (reason)")
    row = _get_pending(db, media_id)
    who = admin.get("username", "admin")
    db.execute(text("""
        UPDATE media SET review_status = 'rejected', reviewed_by = :who,
               reviewed_at = NOW(), reject_reason = :r
        WHERE id = :id
    """), {"who": who, "r": reason, "id": media_id})
    try:
        db.execute(text("""
            INSERT INTO audit_log (event_type, title, detail, actor)
            VALUES ('moderation', :t, :d, :a)
        """), {"t": f"Ролик отклонён: «{row.title}»", "d": f"media_id={media_id}: {reason}", "a": who})
    except Exception:
        pass
    db.commit()
    return {"status": "rejected", "id": media_id, "reason": reason}


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")


@router.patch("/media/{media_id}/duration")
def update_banner_duration(media_id: int, body: dict = Body(...),
                           admin: dict = Depends(require_write),
                           db: Session = Depends(get_db)):
    """Изменить длительность показа баннера-картинки (сек). Для видео
    длительность определяется самим файлом и не редактируется."""
    row = db.execute(text("SELECT filename FROM media WHERE id = :id"), {"id": media_id}).fetchone()
    if not row:
        raise HTTPException(404, "Файл не найден")
    if not row.filename.lower().endswith(_IMAGE_EXTS):
        raise HTTPException(400, "Длительность можно менять только у баннеров-картинок")
    try:
        seconds = int(body.get("seconds"))
    except (TypeError, ValueError):
        raise HTTPException(400, "seconds: ожидается число секунд")
    if not 1 <= seconds <= 300:
        raise HTTPException(400, "seconds: допустимо от 1 до 300")
    db.execute(text("UPDATE media SET duration_seconds = :s WHERE id = :id"),
               {"s": seconds, "id": media_id})
    db.commit()
    return {"status": "ok", "id": media_id, "duration_seconds": seconds}


@router.get("/media/{media_id}/thumbnail")
def media_thumbnail(media_id: int, db: Session = Depends(get_db)):
    """Миниатюра ролика/баннера. Для файлов, загруженных до появления
    миниатюр, генерируется лениво при первом запросе."""
    thumb = os.path.join(THUMBS_DIR, f"{media_id}.jpg")
    if not os.path.exists(thumb):
        row = db.execute(text("SELECT filename FROM media WHERE id = :id"), {"id": media_id}).fetchone()
        if not row:
            raise HTTPException(404, "Файл не найден")
        src = os.path.join(MEDIA_PATH, row.filename)
        if not os.path.exists(src) or not _make_thumbnail(src, media_id):
            raise HTTPException(404, "Миниатюра недоступна")
    return FileResponse(thumb, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ─── Рекламодатели и папки ──────────────────────────────────────────────────

@router.patch("/media/{media_id}/filler")
def toggle_filler(media_id: int, body: dict = Body(...),
                  admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    is_filler = bool(body.get("is_filler", False))
    db.execute(text("UPDATE media SET is_filler=:v WHERE id=:id"), {"v": is_filler, "id": media_id})
    db.commit()
    return {"status": "ok", "is_filler": is_filler}


@router.get("/advertisers")
def list_advertisers(admin: dict = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT a.id, a.name, a.color, a.price_per_minute,
               COALESCE(a.price_per_play, 0) AS price_per_play,
               COALESCE(a.billing_mode, 'per_minute') AS billing_mode,
               COALESCE(a.kind, 'advertiser') AS kind,
               a.note, a.inn, a.contact_person,
               COUNT(DISTINCT f.id) AS folders,
               COUNT(DISTINCT m.id) AS files
        FROM advertisers a
        LEFT JOIN media_folders f ON f.advertiser_id = a.id
        LEFT JOIN media m ON m.advertiser_id = a.id
        GROUP BY a.id ORDER BY a.name
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


# Папки, которые заводятся владельцу контента сразу при регистрации, чтобы
# администратору не приходилось создавать их руками перед первой загрузкой.
# «Видеореклама» — то, что идёт в эфир; «Документы» — сканы договоров,
# деклараций и согласований (в эфир не попадают, см. upload_media).
DEFAULT_OWNER_FOLDERS = ("Видеореклама", "Документы")
DOCS_FOLDER = "Документы"
# Что принимаем в папку «Документы»: сканы и файлы согласований.
DOC_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".xls", ".xlsx", ".odt", ".rtf")


def ensure_owner_folders(db: Session, advertiser_id: int):
    """Создать стандартные папки владельца, если их ещё нет (идемпотентно)."""
    for folder in DEFAULT_OWNER_FOLDERS:
        db.execute(text("""
            INSERT INTO media_folders (advertiser_id, name) VALUES (:a, :n)
            ON CONFLICT (advertiser_id, name) DO NOTHING
        """), {"a": advertiser_id, "n": folder})


@router.post("/advertisers")
def create_advertiser(body: dict = Body(...), admin: dict = Depends(require_write),
                      db: Session = Depends(get_db)):
    """
    Завести владельца контента. kind='gov' — госорган: ролики крутятся так же,
    но денег с него не берут (тарифа нет, счета не выставляются, в дебиторке
    не участвует). Коммерческие рекламодатели заводятся автоматически при
    создании пользователя с ролью «рекламодатель», поэтому руками здесь
    создают в основном госорганы.
    """
    name = (body.get("name") or "").strip()
    color = body.get("color") or "#7fe3c4"
    kind = (body.get("kind") or "advertiser").strip()
    if not name:
        raise HTTPException(400, "name обязателен")
    if kind not in ("advertiser", "gov"):
        raise HTTPException(400, "kind: advertiser или gov")
    try:
        row = db.execute(text(
            "INSERT INTO advertisers (name, color, kind) VALUES (:n, :c, :k) "
            "RETURNING id, name, color, kind, price_per_minute"
        ), {"n": name, "c": color, "k": kind}).fetchone()
        ensure_owner_folders(db, row.id)
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise HTTPException(409, "Владелец с таким именем уже существует")
    return dict(row._mapping)


@router.delete("/advertisers/{adv_id}")
def delete_advertiser(adv_id: int, admin: dict = Depends(require_write),
                      db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM advertisers WHERE id=:id"), {"id": adv_id})
    db.commit()
    return {"status": "deleted"}


@router.patch("/advertisers/{adv_id}/price")
def update_adv_price(adv_id: int, body: dict = Body(...),
                     admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    price = float(body.get("price_per_minute", 0))
    db.execute(text("UPDATE advertisers SET price_per_minute=:p WHERE id=:id"),
               {"p": price, "id": adv_id})
    db.commit()
    return {"status": "ok"}


@router.get("/advertisers/{adv_id}/folders")
def list_folders(adv_id: int, admin: dict = Depends(get_current_admin),
                 db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT f.id, f.name, COUNT(m.id) AS files
        FROM media_folders f
        LEFT JOIN media m ON m.folder_id = f.id
        WHERE f.advertiser_id = :aid
        GROUP BY f.id ORDER BY f.name
    """), {"aid": adv_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/advertisers/{adv_id}/folders")
def create_folder(adv_id: int, body: dict = Body(...),
                  admin: dict = Depends(require_write), db: Session = Depends(get_db)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name обязателен")
    try:
        row = db.execute(text(
            "INSERT INTO media_folders (advertiser_id, name) VALUES (:aid, :n) RETURNING id, name"
        ), {"aid": adv_id, "n": name}).fetchone()
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "Папка с таким именем уже существует")
    return dict(row._mapping)


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, admin: dict = Depends(require_write),
                  db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM media_folders WHERE id=:id"), {"id": folder_id})
    db.commit()
    return {"status": "deleted"}


@router.get("/advertisers/{adv_id}/media")
def list_adv_media(adv_id: int, admin: dict = Depends(get_current_admin),
                   db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT m.id, m.title, m.filename, m.filesize, m.duration_seconds,
               COALESCE(m.is_filler, FALSE) AS is_filler,
               m.folder_id,
               m.category, m.age_rating, m.review_status, m.reject_reason,
               m.status,
               EXISTS(
                   SELECT 1 FROM playlist_items pi WHERE pi.media_id = m.id
               ) AS in_playlist
        FROM media m
        -- Здесь показываем содержимое папок владельца целиком, включая сканы
        -- из «Документов» (status='document'): администратор должен видеть в
        -- папке то, что в неё положил. В эфир они всё равно не идут.
        WHERE m.advertiser_id = :aid AND m.status IN ('ready', 'document')
        ORDER BY m.created_at DESC
    """), {"aid": adv_id}).fetchall()
    return [dict(r._mapping) for r in rows]


# ─── Файлы для мини ПК ──────────────────────────────────────────────────────

# На сколько дней вперёд (кроме сегодняшнего) экран заранее качает ролики
# показов по датам и датовых переопределений.
PRELOAD_DAYS = 7


@router.get("/files/list/{screen_id}")
def files_for_screen(screen_id: int, db: Session = Depends(get_db),
                      _screen=Depends(verify_device_token)):
    """Список файлов, которые мини ПК должен иметь локально.

    Это единственный источник загрузки агента (ds_downloader.sync_files), и
    по нему же ночная очистка агента (ds_cleanup) удаляет всё остальное —
    поле files ленты экрана агент не читает. Сюда входят:
      • плейлисты недельного шаблона — слоты экрана, его группы и всей сети;
      • плейлисты показов по датам (не остановленных, в т. ч. «на все
        экраны») и датовых переопределений экрана и его группы на сегодня и
        PRELOAD_DAYS дней вперёд (МСК) — ролики лежат на диске до начала часа;
      • плейлист действующего общего эфира (broadcast_store.get_broadcast) —
        пока эфир идёт, в т. ч. для экрана, подключившегося позже;
      • все заглушки — их играют часовые блоки, «общий час» и фолбэк.
    Только готовые и одобренные; алкоголь — только для площадок «магазин с
    алколицензией» (38-ФЗ ст. 21). Каждый файл — один раз.
    """
    venue = db.execute(text(f"SELECT COALESCE(venue_type,'{DEFAULT_VENUE}') FROM screens WHERE id = :sid"),
                       {"sid": screen_id}).scalar() or DEFAULT_VENUE
    now = now_msk()
    pids: set[int] = set()       # плейлисты сверх шаблона: показы, переопределения, эфир
    # Ответ с ошибкой агент читает как пустой список, и ночная очистка стёрла
    # бы с экрана все ролики. Поэтому каждая часть — отдельно: сбой одной не
    # убирает другие, а шаблон и заглушки отдаются в любом случае.
    try:
        d_from = now.date()
        d_to = d_from + timedelta(days=PRELOAD_DAYS)
        pids = playlists_for_screen(load_placements(db, d_from, d_to), load_overrides(db, d_from, d_to),
                                    screen_id, d_from, d_to)
    except Exception:
        log.exception("screen %s: плейлисты показов и переопределений для загрузки не получены", screen_id)
        db.rollback()
    try:
        bc = get_broadcast(db, now)
        if bc["is_on"]:
            pids.add(bc["playlist_id"])
    except Exception:
        log.exception("screen %s: плейлист общего эфира для загрузки не получен", screen_id)
        db.rollback()
    rows = db.execute(text(f"""
        SELECT DISTINCT m.id, m.filename, m.filesize, m.md5_hash
        FROM media m
        WHERE m.status = 'ready' AND m.review_status = 'approved'
          AND {venue_sql("m")}
          AND (m.is_filler = TRUE
               OR m.id IN (
                   SELECT pi.media_id FROM playlist_items pi
                   WHERE pi.playlist_id = ANY(:pids)
                      OR pi.playlist_id IN (
                          SELECT playlist_id FROM schedule_slots
                          WHERE playlist_id IS NOT NULL
                            AND (screen_id = :sid
                                 OR (screen_id IS NULL AND group_id IS NOT NULL
                                     AND group_id = (SELECT group_id FROM screens WHERE id = :sid))
                                 OR (screen_id IS NULL AND group_id IS NULL)))))
        ORDER BY m.id
    """), {"sid": screen_id, "venue": venue, "pids": sorted(pids)}).fetchall()
    return [{"media_id": r.id, "filename": r.filename,
             "filesize": r.filesize, "md5_hash": r.md5_hash,
             "download_url": f"/files/download/{r.id}"} for r in rows]


@router.get("/files/download/{media_id}")
def download_file(media_id: int, db: Session = Depends(get_db)):
    """Отдать файл мини ПК для скачивания (Range поддерживается через nginx)"""
    row = db.execute(text("SELECT filename FROM media WHERE id = :id"), {"id": media_id}).fetchone()
    if not row:
        raise HTTPException(404, "Файл не найден")
    filepath = os.path.join(MEDIA_PATH, row.filename)
    if not os.path.exists(filepath):
        raise HTTPException(404, "Файл отсутствует на диске")
    return FileResponse(filepath, media_type="application/octet-stream",
                        filename=row.filename, headers={"Accept-Ranges": "bytes"})
