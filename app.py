import os
import re
import time
import uuid
import shutil
import threading
import traceback
import requests
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)
BASE = Path(__file__).resolve().parent
JOBS_ROOT = BASE / "jobs"
JOBS_ROOT.mkdir(exist_ok=True)

JOBS = {}
LOCK = threading.Lock()

SITE = 'https://shiva.agro.gov.br/pub/comex/qrcodefito'
PATTERN = re.compile(r'(\d{10})\s*/\s*(E\d{10}-[A-Za-z0-9]+)', re.I)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_OK = True
except Exception:
    SELENIUM_OK = False


# ---------- parsing ----------

def parse_messages(text):
    found = []
    for m in PATTERN.finditer(text):
        cf = m.group(1)
        chave = m.group(2).upper()
        lpco = chave.split('-', 1)[0]
        found.append((cf, chave, lpco))
    out, seen = [], set()
    for x in found:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


# ---------- selenium engine (adaptado do CF_SHIVA_original.py para modo headless/servidor) ----------

def make_driver(download_dir: Path):
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1400,1000')
    chrome_bin = os.environ.get('CHROME_BIN')
    if chrome_bin:
        options.binary_location = chrome_bin
    prefs = {
        'download.default_directory': str(download_dir.resolve()),
        'download.prompt_for_download': False,
        'download.directory_upgrade': True,
        'plugins.always_open_pdf_externally': True,
        'safebrowsing.enabled': True,
    }
    options.add_experimental_option('prefs', prefs)
    driver = webdriver.Chrome(options=options)
    try:
        driver.execute_cdp_cmd('Page.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': str(download_dir.resolve()),
        })
    except Exception:
        pass
    return driver


def fill_inputs(driver, cf, chave, log):
    inputs = driver.find_elements(By.CSS_SELECTOR, 'input')
    visible = [x for x in inputs if x.is_displayed() and x.is_enabled()]
    log(f'  Campos encontrados na página: {len(visible)}')
    cf_done = key_done = False
    for el in visible:
        attrs = ' '.join(str(el.get_attribute(a) or '') for a in
                          ['name', 'id', 'placeholder', 'aria-label', 'type']).lower()
        if not cf_done and any(k in attrs for k in ['numero', 'número', 'certificado', 'certificate', 'fito']) \
                and 'senha' not in attrs and 'chave' not in attrs:
            el.clear(); el.send_keys(cf); cf_done = True
        elif not key_done and any(k in attrs for k in ['senha', 'chave', 'access', 'acesso', 'password']):
            el.clear(); el.send_keys(chave); key_done = True
    if not cf_done or not key_done:
        candidates = [x for x in visible if (x.get_attribute('type') or 'text').lower() in ('text', 'number', 'password')]
        if len(candidates) >= 2:
            if not cf_done:
                candidates[0].clear(); candidates[0].send_keys(cf); cf_done = True
            if not key_done:
                candidates[1].clear(); candidates[1].send_keys(chave); key_done = True
    if not (cf_done and key_done):
        raise RuntimeError('Não consegui identificar automaticamente os campos Número e Senha/Chave.')


def click_consult(driver, log):
    texts = ['consultar', 'consulta', 'pesquisar', 'buscar', 'emitir', 'gerar', 'visualizar']
    for b in driver.find_elements(By.CSS_SELECTOR, 'button, input[type=submit], input[type=button], a'):
        if not b.is_displayed() or not b.is_enabled():
            continue
        t = ((b.text or '') + ' ' + (b.get_attribute('value') or '')).strip().lower()
        if any(w in t for w in texts):
            driver.execute_script('arguments[0].click();', b); return
    forms = driver.find_elements(By.TAG_NAME, 'form')
    if forms:
        driver.execute_script("arguments[0].submit();", forms[-1]); return
    raise RuntimeError('Não encontrei o botão de consulta no SHIVA.')


def _save_pdf_bytes(data, target: Path):
    if not data or len(data) < 1000:
        return False
    if not data.startswith(b"%PDF"):
        pos = data.find(b"%PDF")
        if pos >= 0:
            data = data[pos:]
    if not data.startswith(b"%PDF"):
        return False
    target.write_bytes(data)
    return True


def download_pdf_link(driver, target: Path, log):
    links = []
    for a in driver.find_elements(By.CSS_SELECTOR, 'a[href]'):
        href = a.get_attribute('href') or ''
        if '.pdf' in href.lower() and href.startswith('http'):
            links.append(href)
    for el in driver.find_elements(By.CSS_SELECTOR, 'iframe[src],embed[src],object[data]'):
        href = el.get_attribute('src') or el.get_attribute('data') or ''
        if '.pdf' in href.lower() and href.startswith('http'):
            links.append(href)
    for url in dict.fromkeys(links):
        try:
            sess = requests.Session()
            for c in driver.get_cookies():
                sess.cookies.set(c['name'], c['value'], domain=c.get('domain'), path=c.get('path', '/'))
            r = sess.get(url, timeout=30, headers={'User-Agent': driver.execute_script('return navigator.userAgent')})
            if r.ok and _save_pdf_bytes(r.content, target):
                log(f'  PDF baixado diretamente: {url[:180]}')
                return target
        except Exception as e:
            log(f'  Falha ao baixar link PDF: {e}')
    return None


def wait_for_result(driver, log, timeout=35):
    end = time.time() + timeout
    while time.time() < end:
        elems = driver.find_elements(By.CSS_SELECTOR, 'button, a, input[type=button], input[type=submit]')
        texts = []
        for el in elems:
            if el.is_displayed():
                texts.append(((el.text or '') + ' ' + (el.get_attribute('value') or '')).strip().lower())
        if any('baixar pdf' in t or ('baixar' in t and 'pdf' in t) or ('download' in t and 'pdf' in t) for t in texts):
            return True
        if driver.find_elements(By.CSS_SELECTOR, 'a[href*="pdf" i], iframe[src*="pdf" i], embed[src*="pdf" i], object[data*="pdf" i]'):
            return True
        time.sleep(.5)
    return False


def click_download_pdf(driver, log, timeout=20):
    end = time.time() + timeout
    labels = ('baixar pdf', 'baixa pdf', 'download pdf', 'baixar', 'download')
    while time.time() < end:
        candidates = driver.find_elements(By.CSS_SELECTOR, 'button, a, input[type=button], input[type=submit]')
        for el in candidates:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                txt = ((el.text or '') + ' ' + (el.get_attribute('value') or '') + ' ' +
                       (el.get_attribute('aria-label') or '') + ' ' + (el.get_attribute('title') or '')).strip().lower()
                if 'pdf' in txt and any(x in txt for x in labels):
                    log(f'  Botão encontrado: {txt[:100]}')
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    time.sleep(.3)
                    driver.execute_script("arguments[0].click();", el)
                    return True
            except Exception:
                continue
        time.sleep(.5)
    return False


def wait_for_download(downloads: Path, before_files, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        partials = list(downloads.glob('*.crdownload')) + list(downloads.glob('*.tmp'))
        pdfs = []
        for p in downloads.glob('*.pdf'):
            try:
                if p not in before_files and p.stat().st_size > 1000:
                    pdfs.append(p)
            except OSError:
                pass
        if pdfs and not partials:
            return max(pdfs, key=lambda p: p.stat().st_mtime)
        time.sleep(.5)
    return None


# ---------- job runner ----------

def run_job(job_id, text):
    items = parse_messages(text)
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    downloads = job_dir / 'downloads'
    downloads.mkdir(exist_ok=True)

    def log(msg):
        with LOCK:
            JOBS[job_id]['logs'].append(msg)

    with LOCK:
        JOBS[job_id] = {
            'status': 'processando',
            'total': len(items),
            'done': 0,
            'items': [
                {'entrada': f'{cf}/{chave}', 'cf': cf, 'chave': chave, 'lpco': lpco,
                 'status': 'pendente', 'arquivo': None}
                for cf, chave, lpco in items
            ],
            'logs': [],
        }

    if not items:
        with LOCK:
            JOBS[job_id]['status'] = 'concluido'
            JOBS[job_id]['logs'].append('Nenhuma mensagem válida foi encontrada no texto colado.')
        return

    if not SELENIUM_OK:
        with LOCK:
            JOBS[job_id]['status'] = 'erro'
            JOBS[job_id]['logs'].append('ERRO: Selenium não está disponível no servidor.')
        return

    driver = None
    try:
        driver = make_driver(downloads)
        for i, (cf, chave, lpco) in enumerate(items, 1):
            log(f'[{i}/{len(items)}] CF {cf} | LPCO {lpco}')
            try:
                driver.get(SITE)
                WebDriverWait(driver, 20).until(lambda d: d.execute_script('return document.readyState') == 'complete')
                fill_inputs(driver, cf, chave, log)
                before_files = set(downloads.glob('*'))
                click_consult(driver, log)
                if not wait_for_result(driver, log, timeout=35):
                    raise RuntimeError('A consulta foi enviada, mas o botão/link "Baixar PDF" não apareceu no resultado.')
                log('  Resultado localizado. Acionando "Baixar PDF"...')
                clicked = click_download_pdf(driver, log, timeout=20)
                if not clicked:
                    raise RuntimeError('O resultado apareceu, mas não foi possível localizar o botão "Baixar PDF".')
                downloaded = wait_for_download(downloads, before_files, timeout=45)
                filename = f'{lpco}_CF_{cf}.pdf'
                target = job_dir / filename
                if target.exists():
                    target.unlink()
                if downloaded:
                    shutil.move(str(downloaded), str(target))
                    log(f'  ✓ PDF baixado: {filename}')
                else:
                    saved = download_pdf_link(driver, target, log)
                    if not saved:
                        raise RuntimeError('Cliquei em "Baixar PDF", mas o download não apareceu.')
                    log(f'  ✓ PDF recuperado: {filename}')
                with LOCK:
                    JOBS[job_id]['items'][i - 1]['status'] = 'ok'
                    JOBS[job_id]['items'][i - 1]['arquivo'] = filename
            except Exception as e:
                log(f'  ERRO: {e}')
                with LOCK:
                    JOBS[job_id]['items'][i - 1]['status'] = 'erro'
                    JOBS[job_id]['items'][i - 1]['erro'] = str(e)
            finally:
                with LOCK:
                    JOBS[job_id]['done'] = i
        log(f'Processamento concluído. {len(items)} item(ns).')
    except Exception as e:
        log('ERRO GERAL: ' + str(e))
        log(traceback.format_exc())
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        with LOCK:
            JOBS[job_id]['status'] = 'concluido'


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/processar")
def processar():
    data = request.get_json(silent=True) or {}
    text = data.get("texto", "")
    if not text.strip():
        return jsonify({"erro": "Cole pelo menos uma entrada."}), 400
    job_id = uuid.uuid4().hex
    threading.Thread(target=run_job, args=(job_id, text), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/status/<job_id>")
def status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"erro": "Processamento não encontrado."}), 404
    return jsonify(job)


@app.get("/api/download/<job_id>/<path:filename>")
def download(job_id, filename):
    job_dir = JOBS_ROOT / job_id
    return send_from_directory(job_dir, filename, as_attachment=True)


@app.get("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
