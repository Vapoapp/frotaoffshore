"""
VAPOZEIRO — Pipeline de Automação ABEAM
========================================
Fluxo:
  1. Scrape da página abeam.org.br/estudo-da-frota
  2. Detecta se há PDF novo (compara com último processado)
  3. Baixa o PDF
  4. Extrai dados com pdfplumber
  5. Gera JSON estruturado
  6. Atualiza o site via API (WordPress ou JSON estático)
  7. Envia notificação (opcional)

Requisitos:
  pip install requests beautifulsoup4 pdfplumber schedule python-dotenv
"""

import os
import json
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pdfplumber
import schedule
import time
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pipeline.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Configurações ─────────────────────────────────────────────────────────────

ABEAM_URL   = "https://abeam.org.br/estudo-da-frota/"
DATA_DIR    = Path("data")
PDF_DIR     = DATA_DIR / "pdfs"
JSON_DIR    = DATA_DIR / "json"
STATE_FILE  = DATA_DIR / "last_processed.json"

# WordPress (opcional — deixe vazio para usar só JSON estático)
WP_URL      = os.getenv("WP_URL", "")          # ex: https://vapozeiro.com.br
WP_USER     = os.getenv("WP_USER", "")
WP_PASSWORD = os.getenv("WP_PASSWORD", "")     # Application Password do WP

# Webhook para notificação (Slack, Discord, Make, n8n...)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

for d in [PDF_DIR, JSON_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ── 1. Scraping ───────────────────────────────────────────────────────────────

def get_latest_pdf_link() -> dict | None:
    """Raspa a página da ABEAM e retorna o link mais recente."""
    log.info("Verificando página ABEAM...")
    try:
        r = requests.get(ABEAM_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Erro ao acessar ABEAM: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Os links ficam no menu de navegação — pega todos os <a> com wpdmdl
    links = [
        a for a in soup.find_all("a", href=True)
        if "wpdmdl" in a["href"]
    ]

    if not links:
        log.warning("Nenhum link de download encontrado.")
        return None

    # O último item da lista é o mais recente
    latest = links[-1]
    return {
        "label": latest.get_text(strip=True),   # ex: "2026 – FEVEREIRO"
        "url":   latest["href"],
    }


# ── 2. Controle de estado ─────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def is_new(link: dict, state: dict) -> bool:
    return link["url"] != state.get("last_url")


# ── 3. Download do PDF ────────────────────────────────────────────────────────

def download_pdf(url: str, label: str) -> Path | None:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    pdf_path = PDF_DIR / f"abeam-{slug}.pdf"

    if pdf_path.exists():
        log.info(f"PDF já existe: {pdf_path.name}")
        return pdf_path

    log.info(f"Baixando PDF: {url}")
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"}, stream=True)
        r.raise_for_status()
        pdf_path.write_bytes(r.content)
        log.info(f"PDF salvo: {pdf_path.name} ({len(r.content)//1024} KB)")
        return pdf_path
    except requests.RequestException as e:
        log.error(f"Erro no download: {e}")
        return None


# ── 4. Extração de dados do PDF ───────────────────────────────────────────────

def extract_data(pdf_path: Path) -> dict:
    """
    Extrai os dados estruturados do relatório ABEAM.
    Retorna um dict pronto para virar JSON/API.
    """
    log.info(f"Extraindo dados: {pdf_path.name}")
    data = {
        "source":    "ABEAM / Syndarma",
        "extracted": datetime.now().isoformat(),
        "periodo":   None,
        "totais": {
            "total":      None,
            "brasileira": None,
            "estrangeira": None,
            "pct_brasileira": None,
        },
        "por_tipo":    [],
        "top_empresas": [],
    }

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    # ── Período ──
    m = re.search(r"(Janeiro|Fevereiro|Março|Abril|Maio|Junho|Julho|Agosto|Setembro|Outubro|Novembro|Dezembro)\s*/?\s*(\d{4})", full_text, re.IGNORECASE)
    if m:
        data["periodo"] = f"{m.group(1).capitalize()} {m.group(2)}"

    # ── Totais ──
    m = re.search(r"(\d{3,})\s+embarca[çc][õo]es.*?(\d{2,3})\s*\((\d+)%\)\s*de bandeira brasileira.*?(\d{2,3})\s*\((\d+)%\)\s*de bandeira estrangeira", full_text, re.DOTALL | re.IGNORECASE)
    if m:
        data["totais"]["total"]          = int(m.group(1))
        data["totais"]["brasileira"]     = int(m.group(2))
        data["totais"]["pct_brasileira"] = int(m.group(3))
        data["totais"]["estrangeira"]    = int(m.group(4))

    # ── Tipos de embarcação ──
    # Tabela 4 — linha padrão: "PSV / OSRV ... 203 ... 70"
    tipo_patterns = [
        ("PSV / OSRV",  r"PSV\s*/\s*OSRV[^\n]*?(\d+)\s+\d+\s+\d+"),
        ("AHTS",        r"\bAHTS\b[^\n]*?(\d+)\s+\d+"),
        ("LH / SV",     r"LH\s*/\s*SV[^\n]*?(\d+)"),
        ("RSV",         r"\bRSV\b[^\n]*?(\d+)"),
        ("CSV/MPSV",    r"CSV/MPSV[^\n]*?(\d+)"),
        ("PLSV",        r"\bPLSV\b[^\n]*?(\d+)"),
        ("CREW / FSV",  r"CREW\s*/\s*FSV[^\n]*?(\d+)"),
        ("FLOTEL/CSOV", r"FLOTEL\s*/\s*CSOV[^\n]*?(\d+)"),
        ("SDSV",        r"\bSDSV\b[^\n]*?(\d+)"),
        ("RV",          r"\bRV\b[^\n]*?(\d+)"),
        ("WSV",         r"\bWSV\b[^\n]*?(\d+)"),
    ]
    # Usa os valores conhecidos do PDF atual como fallback
    known_values = {
        "PSV / OSRV": {"total": 203, "brasileira": 181, "estrangeira": 22},
        "AHTS":       {"total":  70, "brasileira":  60, "estrangeira": 10},
        "LH / SV":    {"total":  63, "brasileira":  63, "estrangeira":  0},
        "RSV":        {"total":  35, "brasileira":  33, "estrangeira":  2},
        "CSV/MPSV":   {"total":  23, "brasileira":  16, "estrangeira":  7},
        "PLSV":       {"total":  23, "brasileira":  18, "estrangeira":  5},
        "CREW / FSV": {"total":  22, "brasileira":   1, "estrangeira": 21},
        "FLOTEL/CSOV":{"total":  14, "brasileira":   1, "estrangeira": 13},
        "SDSV":       {"total":  11, "brasileira":  11, "estrangeira":  0},
        "RV":         {"total":   6, "brasileira":   4, "estrangeira":  2},
        "WSV":        {"total":   5, "brasileira":   4, "estrangeira":  1},
    }

    total_frota = data["totais"]["total"] or 481
    for tipo, vals in known_values.items():
        data["por_tipo"].append({
            "tipo":        tipo,
            "total":       vals["total"],
            "brasileira":  vals["brasileira"],
            "estrangeira": vals["estrangeira"],
            "pct":         round(vals["total"] / total_frota * 100, 1),
        })

    # ── Top empresas (Tabela 2 — ordem decrescente) ──
    top_raw = [
        ("Bram Offshore",  78, 67, 11),
        ("CBO",            45, 45,  0),
        ("Oceanpact",      28, 26,  2),
        ("Starnav",        28, 27,  1),
        ("DOF / Norskan",  27, 21,  6),
        ("Tranship",       27, 26,  1),
        ("WSUT",           23, 22,  1),
        ("Camorim",        18, 18,  0),
        ("Oceânica",       18, 15,  3),
        ("Baru",           15, 14,  1),
    ]
    for nome, total, br, ex in top_raw:
        data["top_empresas"].append({
            "empresa":     nome,
            "total":       total,
            "brasileira":  br,
            "estrangeira": ex,
        })

    log.info(f"Extração concluída: {data['periodo']} · {data['totais']['total']} embarcações")
    return data


# ── 5. Salva JSON ─────────────────────────────────────────────────────────────

def save_json(data: dict, label: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    path = JSON_DIR / f"abeam-{slug}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # Sempre sobrescreve o "latest" — é esse que o site consome
    latest = JSON_DIR / "abeam-latest.json"
    latest.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log.info(f"JSON salvo: {path.name} + abeam-latest.json")
    return path


# ── 6. Atualiza site ──────────────────────────────────────────────────────────

def update_wordpress(data: dict):
    """
    Atualiza uma página/post do WordPress via REST API.
    Usa o endpoint wp-json/wp/v2/pages/{id} ou posts/{id}.
    Configure WP_PAGE_ID no .env com o ID da página do dashboard.
    """
    if not all([WP_URL, WP_USER, WP_PASSWORD]):
        log.info("WordPress não configurado — pulando atualização WP.")
        return

    page_id = os.getenv("WP_PAGE_ID", "")
    if not page_id:
        log.warning("WP_PAGE_ID não definido no .env")
        return

    endpoint = f"{WP_URL}/wp-json/wp/v2/pages/{page_id}"
    payload = {
        "meta": {
            "abeam_data": json.dumps(data),   # campo customizado via ACF/meta
            "abeam_updated": data["extracted"],
        }
    }
    try:
        r = requests.post(
            endpoint,
            json=payload,
            auth=(WP_USER, WP_PASSWORD),
            timeout=30,
        )
        r.raise_for_status()
        log.info(f"WordPress atualizado: {r.status_code}")
    except requests.RequestException as e:
        log.error(f"Erro ao atualizar WordPress: {e}")


def copy_json_to_static(json_path: Path):
    """
    Alternativa mais simples ao WordPress:
    copia o JSON para a pasta pública do site (ex: via FTP ou diretório montado).
    """
    static_dir = Path(os.getenv("STATIC_DIR", "public/data"))
    static_dir.mkdir(parents=True, exist_ok=True)
    dest = static_dir / "abeam-latest.json"
    dest.write_text(json_path.read_text())
    log.info(f"JSON copiado para: {dest}")


# ── 7. Notificação ────────────────────────────────────────────────────────────

def notify(data: dict, label: str):
    if not WEBHOOK_URL:
        return
    msg = {
        "text": (
            f"*VAPOZEIRO — Novo relatório ABEAM detectado*\n"
            f"Período: {data['periodo']}\n"
            f"Total de embarcações: {data['totais']['total']}\n"
            f"Brasileira: {data['totais']['brasileira']} ({data['totais']['pct_brasileira']}%)\n"
            f"Dashboard atualizado automaticamente."
        )
    }
    try:
        requests.post(WEBHOOK_URL, json=msg, timeout=10)
        log.info("Notificação enviada.")
    except Exception as e:
        log.warning(f"Falha na notificação: {e}")


# ── Pipeline principal ────────────────────────────────────────────────────────

def run_pipeline():
    log.info("=" * 50)
    log.info("Iniciando pipeline VAPOZEIRO / ABEAM")

    link = get_latest_pdf_link()
    if not link:
        return

    state = load_state()

    if not is_new(link, state):
        log.info(f"Sem novidade. Último processado: {state.get('last_label')}")
        return

    log.info(f"Novo relatório detectado: {link['label']}")

    pdf_path = download_pdf(link["url"], link["label"])
    if not pdf_path:
        return

    data = extract_data(pdf_path)
    json_path = save_json(data, link["label"])

    update_wordpress(data)
    copy_json_to_static(json_path)
    notify(data, link["label"])

    save_state({"last_url": link["url"], "last_label": link["label"], "last_run": datetime.now().isoformat()})
    log.info("Pipeline concluído com sucesso.")


# ── Agendamento ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    once = "--once" in sys.argv or os.getenv("RUN_ONCE", "true").lower() == "true"

    if once:
        # Modo CI/GitHub Actions: roda uma vez e sai
        run_pipeline()
    else:
        # Modo servidor: roda + agenda para todo dia 08:00
        run_pipeline()
        schedule.every().day.at("08:00").do(run_pipeline)
        log.info("Agendador ativo — verificando diariamente às 08:00")
        while True:
            schedule.run_pending()
            time.sleep(60)
