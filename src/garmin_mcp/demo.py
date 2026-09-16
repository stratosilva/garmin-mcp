"""Public showcase renderer: synthetic data only, no live account access."""
import json
from pathlib import Path

from .demo_data import demo_data


def demo_html():
    # Reuse the real UI, without its live fetch path or token handling.
    from .dashboard import PAGE_HTML
    assets = Path(__file__).with_name('demo_assets')
    shell = (assets / 'shell.html').read_text(encoding='utf-8')
    css = (assets / 'demo.css').read_text(encoding='utf-8')
    script = (assets / 'demo.js').read_text(encoding='utf-8')
    encoded = json.dumps(demo_data(), ensure_ascii=False).replace('<', '\\u003c')
    intro, story = shell.split('<section id="demo-story"', 1)
    html = PAGE_HTML.replace('<title>Training Dashboard</title>', '<title>Motion, with context · Manuel Silva Gallego</title><meta name="description" content="An interactive AI strategy and data integration case study by Manuel Silva Gallego. Garmin, Basic-Fit and injury context, brought together in a working dashboard.">')
    html = html.replace('</head>', '<meta property="og:title" content="Motion, with context · Manuel Silva Gallego"><meta property="og:description" content="A two-minute interactive tour: Garmin, Basic-Fit and injury context turned into actionable training decisions. Built with ChatGPT."><meta property="og:type" content="website"><style>'+css+'</style></head>')
    html = html.replace('<body>', '<body>'+intro, 1)
    html = html.replace('<script>', '<section id="demo-story"'+story+'<script type="application/json" id="demo-data">'+encoded+'</script><script>'+script+'</script><script>', 1)
    html = html.replace('var TOKEN = new URLSearchParams(location.search).get("token") || "";', 'var TOKEN = "";')
    html = html.replace('.then(render)', '.then(function(data){render(data);demoAfterRender();})', 1)
    html = html.replace('button.textContent="Refresh advice"', 'button.textContent="Sample advice"')
    return html


async def demo_page(request):
    from starlette.responses import HTMLResponse
    return HTMLResponse(demo_html(), headers={
        'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        # No network calls, external images, form submissions or embeds from demo.
        'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
    })
