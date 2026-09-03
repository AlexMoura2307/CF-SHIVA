import os
import re
import io
import zipfile
import time
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

JOBS = {}
LOCK = threading.Lock()

def parse_items(text):
    # Keeps the batch input flexible: each non-empty line is treated as one item.
    # Attempts to extract CF, Chave and LPCO when present.
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cf = re.search(r'\bCF\s*[:=]?\s*([A-Za-z0-9._/-]+)', line, re.I)
        chave = re.search(r'\bChave\s*[:=]?\s*([A-Za-z0-9._/-]+)', line, re.I)
        lpco = re.search(r'\bLPCO\s*[:=]?\s*([A-Za-z0-9._/-]+)', line, re.I)
        items.append({
            "entrada": line,
            "cf": cf.group(1) if cf else "",
            "chave": chave.group(1) if chave else "",
            "lpco": lpco.group(1) if lpco else "",
        })
    return items

def run_job(job_id, text):
    items = parse_items(text)
    with LOCK:
        JOBS[job_id] = {"status": "processando", "total": len(items), "done": 0, "items": items, "logs": []}

    # The original desktop automation is retained in CF_SHIVA_original.py.
    # Selenium/browser automation may require environment-specific SHIVA credentials,
    # selectors and Chrome availability. This worker is intentionally isolated so
    # the web UI remains usable on Render.
    for i, item in enumerate(items, 1):
        with LOCK:
            JOBS[job_id]["logs"].append(f"[{i}/{len(items)}] Recebido: {item['entrada']}")
            JOBS[job_id]["done"] = i
        time.sleep(0.05)

    with LOCK:
        JOBS[job_id]["status"] = "concluido"
        JOBS[job_id]["logs"].append("Processamento concluído. Configure os dados de acesso/automação do SHIVA para habilitar a consulta real.")

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/processar")
def processar():
    data = request.get_json(silent=True) or {}
    text = data.get("texto", "")
    if not text.strip():
        return jsonify({"erro": "Cole pelo menos uma entrada."}), 400
    job_id = str(int(time.time() * 1000000))
    threading.Thread(target=run_job, args=(job_id, text), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.get("/api/status/<job_id>")
def status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"erro": "Processamento não encontrado."}), 404
    return jsonify(job)

@app.get("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
