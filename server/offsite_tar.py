"""Потоковый tar для «Копии на флешку»: архив собирается на лету, в память целиком не попадает.

Чистая логика на stdlib, без БД и без os.environ. Архивы с медиа бывают в
несколько гигабайт, а у контейнера API всего 768 МБ, поэтому мы не пишем tar
через `tarfile.open(...).add()` во временный файл и не держим его в памяти, а
отдаём кусками: заголовок, содержимое файла по частям, добивка до 512 байт.

Размер архива известен заранее (`tar_size`) — из него браузеру сообщают
Content-Length, и скачивание показывает честный прогресс. Для этого поток должен
совпасть с `tar_size` до байта, поэтому и то и другое строится от одних и тех же
заголовков (`_header`).

Формат — PAX: он держит имена длиннее 100 байт и кириллицу в UTF-8 и файлы
больше 8 ГиБ. Такой архив читают и GNU tar на Linux, и bsdtar в Windows.
"""
import tarfile
from dataclasses import dataclass
from typing import Iterator

_BLOCK = tarfile.BLOCKSIZE                      # 512: заголовки и содержимое выровнены по блоку
_END = b"\0" * (2 * _BLOCK)                     # конец архива — два нулевых блока


@dataclass
class TarItem:
    """Один файл будущего архива.

    `size` объявляется заранее и должен совпасть с тем, что отдадут `path` или
    `data`: заголовок с размером уходит раньше содержимого. Файл на диске (`path`)
    читается потоком, небольшой текст (инструкция, контрольные суммы) можно
    передать готовыми байтами (`data`; если заданы оба источника, берётся `data`).
    Пустому файлу (size=0) источник не нужен.
    """
    arcname: str
    size: int
    path: str | None = None
    data: bytes | None = None
    mtime: int = 0
    mode: int = 0o644


def _header(item: TarItem) -> bytes:
    """Заголовок файла в формате PAX (при длинном имени или огромном размере — с расширенным)."""
    if item.data is not None:
        if len(item.data) != item.size:
            raise ValueError(f"{item.arcname}: размер {item.size} не совпал с данными ({len(item.data)})")
    elif item.path is None and item.size != 0:
        raise ValueError(f"{item.arcname}: размер {item.size}, но нет ни файла, ни данных")
    info = tarfile.TarInfo(item.arcname)
    info.size = item.size
    info.mtime = item.mtime
    info.mode = item.mode
    return info.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")


def _padding(size: int) -> int:
    """Сколько нулей добавить после содержимого, чтобы следующий заголовок встал на границу блока."""
    return -size % _BLOCK


def _record_tail(total: int) -> int:
    """Сколько нулей дописать, чтобы весь архив стал кратен tarfile.RECORDSIZE."""
    return -total % tarfile.RECORDSIZE


def tar_size(items: list[TarItem]) -> int:
    """Точный размер того, что отдаст `iter_tar` для этих же элементов.

    Конец архива добивается нулями до целой «записи» tarfile.RECORDSIZE (10240) —
    так делает и сам `tarfile` при закрытии, и утилиты tar ждут именно это.
    """
    body = sum(len(_header(it)) + it.size + _padding(it.size) for it in items) + len(_END)
    return body + _record_tail(body)


def _read_exact(path: str, size: int, chunk: int) -> Iterator[bytes]:
    """Ровно `size` байт файла кусками по `chunk`.

    Файл мог усохнуть или пропасть между подсчётом размера и чтением (его заменили
    или удалили). Заголовок с прежним размером уже ушёл, поэтому недостача байт —
    это тихо битый архив: лучше оборвать выдачу с ошибкой, чем отдать такой.
    Выросший файл не страшен: читаем только объявленное число байт.
    """
    left = size
    with open(path, "rb") as f:
        while left:
            buf = f.read(min(chunk, left))
            if not buf:
                raise RuntimeError(f"{path}: файл стал короче объявленного размера "
                                   f"(не хватило {left} из {size} байт)")
            left -= len(buf)
            yield buf


def iter_tar(items: list[TarItem], chunk: int = 1 << 20) -> Iterator[bytes]:
    """Архив кусками: заголовок, содержимое, добивка до 512, в конце два нулевых блока и добивка до записи."""
    if chunk <= 0:
        raise ValueError("chunk должен быть положительным")
    total = 0
    for it in items:
        header = _header(it)
        yield header
        total += len(header)
        if it.data is not None:
            if it.data:
                yield it.data
        elif it.size:
            yield from _read_exact(it.path, it.size, chunk)
        pad = _padding(it.size)
        if pad:
            yield b"\0" * pad
        total += it.size + pad
    total += len(_END)
    yield _END
    tail = _record_tail(total)
    if tail:
        yield b"\0" * tail


def write_tar(items: list[TarItem], dst_path: str) -> int:
    """То же, что `iter_tar`, но в файл; возвращает размер записанного архива."""
    written = 0
    with open(dst_path, "wb") as f:
        for buf in iter_tar(items):
            f.write(buf)
            written += len(buf)
    return written
