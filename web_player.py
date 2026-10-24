"""Minimal native HTML5 watch page; video bytes come from the Telegram stream."""

from html import escape


def render_watch_page(name, video_url, nonce, mime) -> bytes:
    """Keep the source URL escaped and playback entirely browser-native."""
    title = escape(str(name or "קובץ וידאו"), quote=True)
    url = escape(str(video_url), quote=True)
    nonce = escape(str(nonce), quote=True)
    return (
        '<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="referrer" content="no-referrer">'
        f'<title>{title} | CINEMIX</title>'
        f'<style nonce="{nonce}">'
        'body{margin:0;padding:20px;background:#101114;color:#eee;'
        'font:16px system-ui,sans-serif}main{max-width:1100px;margin:auto}'
        'h1{font-size:20px;overflow-wrap:anywhere}'
        'video{display:block;width:100%;max-height:80vh;background:#000}'
        'a{color:#9fc9ff}p{line-height:1.6}'
        '</style></head><body><main>'
        f'<h1>{title}</h1>'
        f'<video src="{url}" controls playsinline preload="metadata">'
        'הדפדפן אינו תומך בנגן וידאו.</video>'
        f'<p><a href="{url}">פתיחת הקובץ ישירות</a></p>'
        '<p>לחצו על נגן לצפייה. הקובץ מוצג ללא המרה; נדרשת תמיכת הדפדפן בקודק המקורי.</p>'
        '</main></body></html>'
    ).encode("utf-8")
