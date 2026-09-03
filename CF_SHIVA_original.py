import os, re, sys, time, threading, traceback, json, shutil, requests
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

SITE = 'https://shiva.agro.gov.br/pub/comex/qrcodefito'
PATTERN = re.compile(r'(\d{10})\s*/\s*(E\d{10}-[A-Za-z0-9]+)', re.I)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except Exception:
    SELENIUM_OK = False


def desktop_folder():
    d = Path.home() / 'Desktop'
    if not d.exists():
        d = Path.home() / 'Área de Trabalho'
    return d / 'CF SHIVA'


def parse_messages(text):
    found=[]
    for m in PATTERN.finditer(text):
        cf=m.group(1)
        chave=m.group(2).upper()
        lpco=chave.split('-',1)[0]
        found.append((cf,chave,lpco))
    # remove duplicates preserving order
    out=[]; seen=set()
    for x in found:
        if x not in seen:
            out.append(x); seen.add(x)
    return out


def newest_pdf(folder, since):
    pdfs=[]
    for p in folder.glob('*.pdf'):
        try:
            if p.stat().st_mtime >= since - 2:
                pdfs.append(p)
        except OSError: pass
    return max(pdfs, key=lambda p:p.stat().st_mtime) if pdfs else None


def fill_inputs(driver, cf, chave, log):
    # Generic first-version heuristic. It supports common input names/labels without
    # depending on a single SHIVA front-end implementation.
    inputs=driver.find_elements(By.CSS_SELECTOR, 'input')
    visible=[x for x in inputs if x.is_displayed() and x.is_enabled()]
    log(f'  Campos encontrados na página: {len(visible)}')
    cf_done=key_done=False
    for el in visible:
        attrs=' '.join(str(el.get_attribute(a) or '') for a in ['name','id','placeholder','aria-label','type']).lower()
        if not cf_done and any(k in attrs for k in ['numero','número','certificado','certificate','fito']) and 'senha' not in attrs and 'chave' not in attrs:
            el.clear(); el.send_keys(cf); cf_done=True
        elif not key_done and any(k in attrs for k in ['senha','chave','access','acesso','password']):
            el.clear(); el.send_keys(chave); key_done=True
    # Fallback: use first two visible text/password-like inputs.
    if not cf_done or not key_done:
        candidates=[x for x in visible if (x.get_attribute('type') or 'text').lower() in ('text','number','password')]
        if len(candidates)>=2:
            if not cf_done:
                candidates[0].clear(); candidates[0].send_keys(cf); cf_done=True
            if not key_done:
                candidates[1].clear(); candidates[1].send_keys(chave); key_done=True
    if not (cf_done and key_done):
        raise RuntimeError('Não consegui identificar automaticamente os campos Número e Senha/Chave.')


def click_consult(driver, log):
    # Try buttons by common text; otherwise submit the last form.
    texts=['consultar','consulta','pesquisar','buscar','emitir','gerar','visualizar']
    for b in driver.find_elements(By.CSS_SELECTOR, 'button, input[type=submit], input[type=button], a'):
        if not b.is_displayed() or not b.is_enabled(): continue
        t=((b.text or '')+' '+(b.get_attribute('value') or '')).strip().lower()
        if any(w in t for w in texts):
            driver.execute_script('arguments[0].click();', b); return
    forms=driver.find_elements(By.TAG_NAME,'form')
    if forms:
        driver.execute_script("arguments[0].submit();", forms[-1]); return
    raise RuntimeError('Não encontrei o botão de consulta no SHIVA.')


def _save_pdf_bytes(data, target):
    if not data or len(data) < 1000:
        return False
    if not data.startswith(b"%PDF"):
        # Some responses may include whitespace/BOM before the PDF signature.
        pos=data.find(b"%PDF")
        if pos >= 0:
            data=data[pos:]
    if not data.startswith(b"%PDF"):
        return False
    target.write_bytes(data)
    return True


def capture_pdf_from_network(driver, target, log, timeout=15):
    """Capture a PDF response from Chrome DevTools when SHIVA opens the PDF in-browser."""
    deadline=time.time()+timeout
    seen=set()
    while time.time() < deadline:
        try:
            entries=driver.get_log('performance')
        except Exception:
            entries=[]
        for entry in entries:
            try:
                msg=json.loads(entry['message'])['message']
                if msg.get('method') != 'Network.responseReceived':
                    continue
                params=msg.get('params',{})
                resp=params.get('response',{})
                mime=(resp.get('mimeType') or '').lower()
                url=resp.get('url') or ''
                req_id=params.get('requestId')
                if req_id in seen:
                    continue
                if 'application/pdf' not in mime and not url.lower().split('?',1)[0].endswith('.pdf'):
                    continue
                seen.add(req_id)
                log(f'  PDF detectado no navegador: {url[:180]}')
                try:
                    resp_body=driver.execute_cdp_cmd('Network.getResponseBody', {'requestId':req_id})
                    body=resp_body.get('body','')
                    raw=body
                    if resp_body.get('base64Encoded'):
                        import base64
                        raw=base64.b64decode(body)
                    elif isinstance(body,str):
                        raw=body.encode('latin1','ignore')
                    if _save_pdf_bytes(raw,target):
                        return target
                except Exception as e:
                    log(f'  Não foi possível obter corpo via DevTools: {e}')
                # Fallback: download the response URL using Chrome's cookies.
                if url.startswith('http'):
                    try:
                        sess=requests.Session()
                        for c in driver.get_cookies():
                            sess.cookies.set(c['name'],c['value'],domain=c.get('domain'),path=c.get('path','/'))
                        r=sess.get(url,timeout=30,headers={'User-Agent':driver.execute_script('return navigator.userAgent')})
                        if r.ok and _save_pdf_bytes(r.content,target):
                            return target
                    except Exception as e:
                        log(f'  Fallback HTTP do PDF falhou: {e}')
            except Exception:
                continue
        time.sleep(.5)
    return None


def download_pdf_link(driver, target, log):
    """Download a discovered PDF href directly, preserving SHIVA session cookies."""
    links=[]
    for a in driver.find_elements(By.CSS_SELECTOR,'a[href]'):
        href=a.get_attribute('href') or ''
        if '.pdf' in href.lower() and href.startswith('http'):
            links.append(href)
    # Also inspect embeds/iframes/object tags used by PDF viewers.
    for el in driver.find_elements(By.CSS_SELECTOR,'iframe[src],embed[src],object[data]'):
        href=el.get_attribute('src') or el.get_attribute('data') or ''
        if '.pdf' in href.lower() and href.startswith('http'):
            links.append(href)
    for url in dict.fromkeys(links):
        try:
            sess=requests.Session()
            for c in driver.get_cookies():
                sess.cookies.set(c['name'],c['value'],domain=c.get('domain'),path=c.get('path','/'))
            r=sess.get(url,timeout=30,headers={'User-Agent':driver.execute_script('return navigator.userAgent')})
            if r.ok and _save_pdf_bytes(r.content,target):
                log(f'  PDF baixado diretamente: {url[:180]}')
                return target
        except Exception as e:
            log(f'  Falha ao baixar link PDF: {e}')
    return None


def wait_for_result(driver, log, timeout=30):
    """Wait until the consultation result is rendered."""
    end=time.time()+timeout
    while time.time()<end:
        # The result page may contain the PDF download control as button, link or input.
        elems=driver.find_elements(By.CSS_SELECTOR,'button, a, input[type=button], input[type=submit]')
        texts=[]
        for el in elems:
            if el.is_displayed():
                texts.append(((el.text or '')+' '+(el.get_attribute('value') or '')).strip().lower())
        if any('baixar pdf' in t or ('baixar' in t and 'pdf' in t) or ('download' in t and 'pdf' in t) for t in texts):
            return True
        # Also allow PDF/result links to be present even if the button text differs.
        if driver.find_elements(By.CSS_SELECTOR, 'a[href*="pdf" i], iframe[src*="pdf" i], embed[src*="pdf" i], object[data*="pdf" i]'):
            return True
        time.sleep(.5)
    return False


def click_download_pdf(driver, log, timeout=30):
    """Find and click SHIVA's own 'Baixar PDF' control. Returns True if clicked."""
    end=time.time()+timeout
    labels=('baixar pdf','baixa pdf','download pdf','baixar','download')
    while time.time()<end:
        candidates=driver.find_elements(By.CSS_SELECTOR,'button, a, input[type=button], input[type=submit]')
        for el in candidates:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                txt=((el.text or '')+' '+(el.get_attribute('value') or '')+' '+
                     (el.get_attribute('aria-label') or '')+' '+(el.get_attribute('title') or '')).strip().lower()
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


def wait_for_download(downloads, before_files, timeout=40):
    """Wait for a new completed PDF in Chrome's configured download folder."""
    end=time.time()+timeout
    while time.time()<end:
        partials=list(downloads.glob('*.crdownload'))+list(downloads.glob('*.tmp'))
        pdfs=[]
        for p in downloads.glob('*.pdf'):
            try:
                if p not in before_files and p.stat().st_size>1000:
                    pdfs.append(p)
            except OSError:
                pass
        if pdfs and not partials:
            return max(pdfs,key=lambda p:p.stat().st_mtime)
        time.sleep(.5)
    return None


def run_batch(items, log, progress):
    if not SELENIUM_OK:
        raise RuntimeError('Selenium não está instalado. Execute INSTALAR.bat antes da primeira utilização.')
    out=desktop_folder(); out.mkdir(parents=True, exist_ok=True)
    downloads=Path.cwd()/'downloads'; downloads.mkdir(exist_ok=True)
    options=webdriver.ChromeOptions()
    prefs={
        'download.default_directory':str(downloads.resolve()),
        'download.prompt_for_download':False,
        'download.directory_upgrade':True,
        'plugins.always_open_pdf_externally':True,
        'download.extensions_to_open':'',
        'safebrowsing.enabled':True,
    }
    options.add_experimental_option('prefs',prefs)
    options.add_argument('--start-maximized')
    driver=webdriver.Chrome(options=options)
    try:
        total=len(items)
        for i,(cf,chave,lpco) in enumerate(items,1):
            progress(i-1,total)
            log(f'[{i}/{total}] CF {cf} | LPCO {lpco}')
            driver.get(SITE)
            WebDriverWait(driver,20).until(lambda d: d.execute_script('return document.readyState')=='complete')
            fill_inputs(driver,cf,chave,log)
            # Snapshot files before this process so an old PDF can never be mistaken for the new one.
            before_files=set(downloads.glob('*'))
            click_consult(driver,log)
            if not wait_for_result(driver,log,timeout=35):
                raise RuntimeError('A consulta foi enviada, mas o botão/link "Baixar PDF" não apareceu no resultado.')
            log('  Resultado localizado. Acionando automaticamente "Baixar PDF"...')
            clicked=click_download_pdf(driver,log,timeout=20)
            if not clicked:
                raise RuntimeError('O resultado apareceu, mas não foi possível localizar o botão "Baixar PDF".')
            log('  ✓ Clique automático em "Baixar PDF" realizado.')
            downloaded=wait_for_download(downloads,before_files,timeout=45)
            target=out/f'{lpco}_CF_{cf}.pdf'
            if target.exists(): target.unlink()
            if downloaded:
                try:
                    downloaded.replace(target)
                except Exception:
                    shutil.copy2(downloaded,target); downloaded.unlink(missing_ok=True)
                log(f'  ✓ PDF baixado automaticamente: {target}')
            else:
                # Fallback only if the browser exposed a direct PDF link; no manual click is required.
                saved=download_pdf_link(driver,target,log)
                if not saved:
                    raise RuntimeError('Cliquei automaticamente em "Baixar PDF", mas o download não apareceu na pasta de downloads.')
                log(f'  ✓ PDF recuperado automaticamente: {target}')
            progress(i,total)
        log(f'Finalizado. Arquivos em: {out}')
    finally:
        driver.quit()


class App:
    def __init__(self, root):
        self.root=root; root.title('CF SHIVA - Extrator de Certificados - V3'); root.geometry('920x680')
        try:
            import os
            icon_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CF_SHIVA.ico')
            if os.path.exists(icon_path):
                root.iconbitmap(icon_path)
        except Exception:
            pass
        top=ttk.Frame(root,padding=10); top.pack(fill='both',expand=True)
        ttk.Label(top,text='Cole as mensagens do SHIVA abaixo (pode colar vários processos):',font=('Segoe UI',11,'bold')).pack(anchor='w')
        self.txt=scrolledtext.ScrolledText(top,height=13,font=('Consolas',10)); self.txt.pack(fill='both',expand=False,pady=8)
        self.txt.insert('1.0','Mensagem: Certificado(s) Fitossanitário(s) emitido(s) (Número / Chaves de Acesso): 2600098167/E2600462864-3ETEWRAM. Consulta pelo site: https://shiva.agro.gov.br/pub/comex/qrcodefito')
        bar=ttk.Frame(top); bar.pack(fill='x')
        self.btn=ttk.Button(bar,text='PROCESSAR CFs',command=self.start); self.btn.pack(side='left')
        ttk.Button(bar,text='Limpar',command=lambda:self.txt.delete('1.0','end')).pack(side='left',padx=8)
        self.info=ttk.Label(bar,text=''); self.info.pack(side='left',padx=10)
        self.pb=ttk.Progressbar(top,mode='determinate'); self.pb.pack(fill='x',pady=8)
        ttk.Label(top,text='Log:').pack(anchor='w')
        self.logbox=scrolledtext.ScrolledText(top,height=14,font=('Consolas',9)); self.logbox.pack(fill='both',expand=True)
        self.logbox.insert('end','V3 pronta. O botão \"Baixar PDF\" será acionado automaticamente.\n')
    def log(self,s): self.root.after(0,lambda:(self.logbox.insert('end',s+'\n'),self.logbox.see('end')))
    def progress(self,n,total): self.root.after(0,lambda:(self.pb.config(maximum=total,value=n),self.info.config(text=f'{n}/{total}')))
    def start(self):
        items=parse_messages(self.txt.get('1.0','end'))
        if not items: messagebox.showwarning('Atenção','Nenhuma mensagem válida foi encontrada.'); return
        self.log('Processos identificados:')
        for cf,ch,lp in items: self.log(f'  CF={cf} | Chave={ch} | LPCO={lp}')
        self.btn.config(state='disabled'); self.pb.config(value=0,maximum=len(items))
        def work():
            try: run_batch(items,self.log,self.progress); self.root.after(0,lambda:messagebox.showinfo('Concluído',f'Processamento terminado.\nPasta: {desktop_folder()}'))
            except Exception as e:
                self.log('ERRO: '+str(e)); self.log(traceback.format_exc()); self.root.after(0,lambda:messagebox.showerror('Erro',str(e)))
            finally: self.root.after(0,lambda:self.btn.config(state='normal'))
        threading.Thread(target=work,daemon=True).start()

if __name__=='__main__':
    root=tk.Tk(); App(root); root.mainloop()
