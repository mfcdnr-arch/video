"""
Проверка медиафайла перед сохранением в медиатеку: валидность, длительность,
кодек, разрешение. Через ffprobe (уже стоит в server/Dockerfile).

Принимает и видео, и статичные изображения (JPG/PNG-баннеры) — mpv на
агенте показывает картинки сам (--image-display-duration). Длительность
показа баннера фиксированная (IMAGE_DURATION), она же пишется в
media.duration_seconds, чтобы биллинг и часовой микс считали баннеры
той же длительностью, которой их реально показывает плеер.
"""
import json
import subprocess

# Секунды показа одного баннера. Должно совпадать с
# --image-display-duration в agent/ds_player.py.
IMAGE_DURATION = 10.0

# Кодеки, которыми ffprobe представляет статичные изображения.
_IMAGE_CODECS = {"mjpeg", "png", "bmp", "webp", "gif", "tiff"}


def check_video(filepath: str) -> dict:
    """
    Возвращает: {"ok": bool, "error": str|None, "duration": float,
                 "codec": str|None, "width": int|None, "height": int|None,
                 "is_image": bool}
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", filepath],
            capture_output=True, timeout=30, text=True,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Проверка файла заняла слишком много времени"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe не найден на сервере"}

    if result.returncode != 0:
        return {"ok": False, "error": "Файл повреждён или не является видео"}

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Не удалось разобрать метаданные файла"}

    video_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video_stream:
        return {"ok": False, "error": "В файле не найдена видеодорожка или изображение"}

    codec = video_stream.get("codec_name")
    is_image = codec in _IMAGE_CODECS

    if is_image:
        # Статичный баннер: длительность показа задаётся плеером.
        return {
            "ok": True,
            "error": None,
            "duration": IMAGE_DURATION,
            "codec": codec,
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "is_image": True,
        }

    duration_raw = info.get("format", {}).get("duration") or video_stream.get("duration")
    try:
        duration = float(duration_raw) if duration_raw else 0.0
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        return {"ok": False, "error": "Не удалось определить длительность ролика"}

    # Мягкие предупреждения (не блокируют загрузку): mpv на Astra Linux
    # надёжнее всего играет H.264 до 1080p; HEVC/4K на слабом мини-ПК
    # могут тормозить или не декодироваться вовсе.
    warnings = []
    if codec not in ("h264",):
        warnings.append(f"Кодек {codec or 'неизвестен'} — на мини-ПК надёжнее H.264; "
                        "проверьте воспроизведение на реальном экране")
    height = video_stream.get("height") or 0
    if height > 1080:
        warnings.append(f"Разрешение {video_stream.get('width')}x{height} выше 1080p — "
                        "слабый мини-ПК может не потянуть, рекомендуем 1920x1080")

    return {
        "ok": True,
        "error": None,
        "warning": "; ".join(warnings) if warnings else None,
        "duration": duration,
        "codec": codec,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "is_image": False,
    }
