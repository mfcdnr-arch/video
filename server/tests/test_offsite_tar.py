"""Потоковый tar для «Копии на флешку» (offsite_tar).

Архивы с медиа бывают в несколько гигабайт, а у API 768 МБ памяти, поэтому tar
собирается потоком. Главное, что проверяем: заявленный размер (Content-Length)
совпадает с фактическим потоком до байта, а получившийся архив читают настоящие
читалки — tarfile и GNU tar (на Windows тот же формат читает bsdtar).
"""
import io
import shutil
import subprocess
import tarfile

import pytest

from offsite_tar import TarItem, iter_tar, tar_size, write_tar

MIB = 1 << 20


def _make_files(tmp_path):
    """Три файла «неудобных» размеров (пустой, на байт меньше блока, больше мегабайта) и вставка из памяти."""
    sizes = {"empty.bin": 0, "almost_block.bin": 511, "big.bin": MIB + 1}
    items = []
    contents = {}
    for i, (name, size) in enumerate(sizes.items()):
        body = bytes((j * 7 + i) % 251 for j in range(size))
        f = tmp_path / name
        f.write_bytes(body)
        arc = "media/" + name
        items.append(TarItem(arc, size, path=str(f), mtime=1_700_000_000 + i, mode=0o640))
        contents[arc] = body
    inline = "README: привет\n".encode("utf-8")
    items.append(TarItem("README.txt", len(inline), data=inline, mtime=1_700_000_100, mode=0o600))
    contents["README.txt"] = inline
    return items, contents


def test_size_matches_stream_and_tarfile_reads(tmp_path):
    items, contents = _make_files(tmp_path)
    blob = b"".join(iter_tar(items))
    assert len(blob) == tar_size(items)
    assert len(blob) % tarfile.RECORDSIZE == 0

    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        members = tf.getmembers()
        assert [m.name for m in members] == [it.arcname for it in items]
        for m, it in zip(members, items):
            assert m.isfile()
            assert m.size == it.size
            assert m.mode == it.mode
            assert m.mtime == it.mtime
            assert tf.extractfile(m).read() == contents[m.name]


def test_small_chunk_gives_same_bytes(tmp_path):
    items, _ = _make_files(tmp_path)
    assert b"".join(iter_tar(items, chunk=4096)) == b"".join(iter_tar(items))


def test_empty_archive_is_one_record():
    # два нулевых блока, добитые до записи 10240, — как у обычного tar
    assert tar_size([]) == tarfile.RECORDSIZE
    assert b"".join(iter_tar([])) == b"\0" * tarfile.RECORDSIZE


def test_long_and_cyrillic_names(tmp_path):
    name = "media/" + "Ролик_" * 30 + ".mp4"
    assert len(name.encode("utf-8")) > 100
    f = tmp_path / "x.mp4"
    f.write_bytes(b"video-bytes")
    items = [TarItem(name, 11, path=str(f))]
    blob = b"".join(iter_tar(items))
    assert len(blob) == tar_size(items)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        assert tf.getnames() == [name]
        assert tf.extractfile(name).read() == b"video-bytes"


def test_write_tar_equals_iter(tmp_path):
    items, _ = _make_files(tmp_path)
    dst = tmp_path / "out.tar"
    written = write_tar(items, str(dst))
    assert written == tar_size(items)
    assert dst.read_bytes() == b"".join(iter_tar(items))


def test_shrunk_file_raises(tmp_path):
    # файл усох после того, как мы объявили его размер, — тихо битый архив недопустим
    f = tmp_path / "shrunk.bin"
    f.write_bytes(b"x" * 100)
    items = [TarItem("media/shrunk.bin", 200, path=str(f))]
    with pytest.raises(RuntimeError):
        b"".join(iter_tar(items))


def test_grown_file_is_cut_to_declared_size(tmp_path):
    # файл вырос (его дописывают): берём ровно объявленный размер, архив остаётся целым
    f = tmp_path / "grown.bin"
    f.write_bytes(b"0123456789" * 100)
    items = [TarItem("media/grown.bin", 10, path=str(f))]
    blob = b"".join(iter_tar(items))
    assert len(blob) == tar_size(items)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        assert tf.extractfile("media/grown.bin").read() == b"0123456789"


def test_inline_data_size_mismatch_raises():
    with pytest.raises(ValueError):
        tar_size([TarItem("a.txt", 5, data=b"abc")])


def test_missing_source_with_nonzero_size_raises():
    with pytest.raises(ValueError):
        tar_size([TarItem("a.bin", 5)])


def test_zero_size_item_without_source_is_empty_file():
    items = [TarItem("empty.txt", 0)]
    blob = b"".join(iter_tar(items))
    assert len(blob) == tar_size(items)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        assert tf.extractfile("empty.txt").read() == b""


@pytest.mark.skipif(shutil.which("tar") is None, reason="нет утилиты tar")
def test_system_tar_extracts_archive(tmp_path):
    # человек на флешке распаковывает не питоном, а своим tar — проверяем его же
    src = tmp_path / "src"
    src.mkdir()
    items, contents = _make_files(src)
    # длиннее 100 байт (нужен PAX), но короче 255 — предела имени файла в ФС, иначе tar не создаст файл
    long_name = "media/" + "Ролик_" * 12 + ".mp4"
    assert 100 < len(long_name.encode("utf-8")) < 255
    items.append(TarItem(long_name, 3, data=b"abc"))
    contents[long_name] = b"abc"
    arc = tmp_path / "check.tar"
    write_tar(items, str(arc))
    out = tmp_path / "extracted"
    out.mkdir()
    res = subprocess.run(["tar", "-xf", str(arc), "-C", str(out)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    for arcname, body in contents.items():
        assert (out / arcname).read_bytes() == body
