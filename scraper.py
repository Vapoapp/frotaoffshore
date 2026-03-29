import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("pipeline.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

ABEAM_URL = "https://abeam.org.br/estudo-da-frota/"
DATA_DIR = Path("data")
PDF_DIR = DATA_DIR / "pdfs"
JSON_DIR = DATA_DIR / "json"
STATE_FILE = DATA_DIR / "last_processed.json"

WP_URL = os.getenv("WP_URL", "")
WP_USER = os.getenv("WP_USER", "")
WP_PASSWORD = os.getenv("WP_PASSWORD", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
STATIC_DIR = Path(os.getenv("STATIC_DIR", "public/data"))

for d in [PDF_DIR, JSON_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

TYPE_ORDER = [
    "PSV / OSRV", "AHTS", "LH / SV", "RSV", "CSV/MPSV", "PLSV",
    "CREW / FSV", "FLOTEL/CSOV", "SDSV", "RV", "WSV", "HLV",
    "DLV", "OTSV", "DSV",
]

FOREIGN_TYPE_ORDER = [
    "PSV / OSRV", "PLSV", "CSV/MPSV", "FLOTEL/CSOV", "AHTS", "RV",
    "HLV", "DLV", "RSV", "WSV", "CREW / FSV",
]

MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}
MONTHS_PT_CAP = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

OFFICIAL_HISTORY = [
    {"periodo": "1985", "label": "1985", "ano": 1985, "mes": 1, "total": 108, "brasileira": 13, "estrangeira": 95},
    {"periodo": "1997", "label": "1997", "ano": 1997, "mes": 1, "total": 137, "brasileira": 32, "estrangeira": 105},
    {"periodo": "2004", "label": "2004", "ano": 2004, "mes": 1, "total": 155, "brasileira": 65, "estrangeira": 90},
    {"periodo": "2007", "label": "2007", "ano": 2007, "mes": 1, "total": 205, "brasileira": 112, "estrangeira": 93},
    {"periodo": "2014", "label": "2014", "ano": 2014, "mes": 1, "total": 500, "brasileira": 243, "estrangeira": 257},
    {"periodo": "2024", "label": "2024", "ano": 2024, "mes": 1, "total": 453, "brasileira": 382, "estrangeira": 71},
    {"periodo": "Fevereiro 2026", "label": "Fev/26", "ano": 2026, "mes": 2, "total": 481, "brasileira": 390, "estrangeira": 91},
]


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize_text(text: str) -> str:
    text = strip_accents(text.lower())
    return re.sub(r"\s+", " ", text).strip()


def format_period_label(year: int, month: int | None) -> tuple[str, str]:
    if month and month in MONTHS_PT_CAP:
        full = f"{MONTHS_PT_CAP[month]} {year}"
        short = f"{MONTHS_PT_CAP[month][:3]}/{str(year)[-2:]}"
        return full, short
    y = str(year)
    return y, y


def extract_month_year(text: str) -> tuple[int, int] | None:
    norm = normalize_text(text)
    for month_name, month_num in MONTHS_PT.items():
        m = re.search(rf"\b{re.escape(month_name)}\b[\s/_-]*(\d{{4}})", norm)
        if m:
            return int(m.group(1)), month_num
    m = re.search(r"\b(20\d{2}|19\d{2})\b", norm)
    if m:
        return int(m.group(1)), 1
    return None


def get_latest_pdf_link() -> dict | None:
    log.info("Verificando página ABEAM...")
    try:
        r = requests.get(ABEAM_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Erro ao acessar ABEAM: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "wpdmdl" not in href:
            continue
        label = a.get_text(" ", strip=True) or href
        ym = extract_month_year(f"{label} {href}")
        links.append({"label": label, "url": href, "ym": ym})

    if not links:
        log.warning("Nenhum link de download encontrado.")
        return None

    dated = [item for item in links if item["ym"] is not None]
    if dated:
        dated.sort(key=lambda x: x["ym"], reverse=True)
        latest = dated[0]
    else:
        latest = links[-1]

    log.info("PDF escolhido: %s", latest["label"])
    return {"label": latest["label"], "url": latest["url"]}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_new(link: dict, state: dict) -> bool:
    return link["url"] != state.get("last_url")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(text)).strip("-")


def download_pdf(url: str, label: str) -> Path | None:
    pdf_path = PDF_DIR / f"abeam-{slugify(label)}.pdf"
    if pdf_path.exists():
        log.info(f"PDF já existe: {pdf_path.name}")
        return pdf_path
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        pdf_path.write_bytes(r.content)
        log.info(f"PDF salvo: {pdf_path.name}")
        return pdf_path
    except requests.RequestException as e:
        log.error(f"Erro no download do PDF: {e}")
        return None


def read_pages(pdf_path: Path) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        return [(page.extract_text() or "") for page in pdf.pages]


def extract_period_and_totals(pages: list[str]) -> tuple[str, dict]:
    text = "\n".join(pages)
    norm = normalize_text(text)

    period_match = None
    for month_name in MONTHS_PT:
        m = re.search(rf"\b({month_name})\b\s*/?\s*(\d{{4}})", norm, re.IGNORECASE)
        if m:
            period_match = m
            break

    period = None
    if period_match:
        month = MONTHS_PT[period_match.group(1)]
        year = int(period_match.group(2))
        period, _ = format_period_label(year, month)

    totals_match = re.search(
        r"(\d+)\s+embarcac(?:oes|oes),\s*(\d+)\s*\((\d+)%\)\s*de bandeira brasileira\s*e\s*(\d+)\s*\((\d+)%\)\s*de bandeira estrangeira",
        norm,
        re.IGNORECASE,
    )
    if not totals_match:
        raise ValueError("Não foi possível extrair os totais gerais do PDF.")

    totals = {
        "total": int(totals_match.group(1)),
        "brasileira": int(totals_match.group(2)),
        "pct_brasileira": int(totals_match.group(3)),
        "estrangeira": int(totals_match.group(4)),
    }
    return period, totals


def ints_from_line(line: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", line)]


def score_page(page_text: str, keywords: list[str]) -> int:
    norm = normalize_text(page_text)
    return sum(1 for kw in keywords if kw in norm)


def parse_total_line(page_text: str, expected_last: int, min_numbers: int) -> list[int] | None:
    for raw_line in page_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if "total" not in normalize_text(line):
            continue
        nums = ints_from_line(line)
        if nums and nums[-1] == expected_last and len(nums) >= min_numbers:
            return nums
    return None


def find_page_with_total(pages: list[str], expected_last: int, min_numbers: int, preferred_keywords: list[str]) -> str:
    ranked_pages = sorted(
        pages,
        key=lambda page: score_page(page, preferred_keywords),
        reverse=True,
    )
    for page in ranked_pages:
        nums = parse_total_line(page, expected_last, min_numbers)
        if nums:
            return page

    for page in pages:
        nums = parse_total_line(page, expected_last, min_numbers)
        if nums:
            return page

    raise ValueError(f"Página com linha Total {expected_last} não encontrada.")


def parse_types(pages: list[str], totals: dict) -> list[dict]:
    total_page = find_page_with_total(
        pages,
        totals["total"],
        16,
        ["tipo", "frota", "psv", "ahts", "plsv", "csv", "sdsv"],
    )
    total_counts = parse_total_line(total_page, totals["total"], 16)[:-1]
    if len(total_counts) != len(TYPE_ORDER):
        raise ValueError("Contagem total por tipo não bate com o número esperado de colunas.")

    foreign_page = find_page_with_total(
        pages,
        totals["estrangeira"],
        12,
        ["bandeira estrangeira", "estrangeira", "plsv", "csv", "frota"],
    )
    foreign_counts = parse_total_line(foreign_page, totals["estrangeira"], 12)[:-1]
    if len(foreign_counts) != len(FOREIGN_TYPE_ORDER):
        raise ValueError("Contagem estrangeira por tipo não bate com o número esperado de colunas.")

    total_map = dict(zip(TYPE_ORDER, total_counts))
    foreign_map = {k: 0 for k in TYPE_ORDER}
    foreign_map.update(dict(zip(FOREIGN_TYPE_ORDER, foreign_counts)))

    result = []
    for t in TYPE_ORDER:
        total = total_map[t]
        foreign = foreign_map[t]
        brazilian = total - foreign
        if brazilian < 0:
            raise ValueError(f"Tipo {t} ficou com bandeira brasileira negativa.")
        result.append(
            {
                "tipo": t,
                "total": total,
                "brasileira": brazilian,
                "estrangeira": foreign,
                "pct": round(total / totals["total"] * 100, 1),
            }
        )
    return result


def parse_company_line(line: str) -> dict | None:
    norm = normalize_text(line)
    if norm.startswith("total") or "empresa status total" in norm or norm.startswith("bandeira"):
        return None

    m = re.match(r"^(.*?)\s+(ABEAM|N[aã]o Associado)\s+((?:\d+\s+){1,2}\d+)\s*$", line, re.IGNORECASE)
    if not m:
        return None

    empresa = m.group(1).strip()
    status = "Não Associado" if normalize_text(m.group(2)) == "nao associado" else "ABEAM"
    nums = [int(x) for x in m.group(3).split()]
    if len(nums) == 2:
        brasileira, total = nums
        estrangeira = 0
    elif len(nums) == 3:
        brasileira, estrangeira, total = nums
    else:
        return None

    return {
        "empresa": empresa.title().replace("Dof / Norskan", "DOF / Norskan").replace("Wsut", "WSUT").replace("Cbo", "CBO"),
        "status": status,
        "brasileira": brasileira,
        "estrangeira": estrangeira,
        "total": total,
    }


def parse_companies(pages: list[str], totals: dict) -> tuple[list[dict], int]:
    best_companies: list[dict] = []
    for page in pages:
        companies = []
        for line in page.splitlines():
            row = parse_company_line(line)
            if row:
                companies.append(row)
        if len(companies) > len(best_companies):
            best_companies = companies

    companies = best_companies
    if not companies:
        raise ValueError("Não foi possível extrair a tabela de empresas.")
    total_empresas = len(companies)
    if sum(c["total"] for c in companies) != totals["total"]:
        raise ValueError("Soma das empresas não bate com o total da frota.")
    top = sorted(companies, key=lambda x: (-x["total"], x["empresa"]))[:10]
    return top, total_empresas


def historico_key(item: dict) -> tuple[int, int]:
    return int(item.get("ano", 0)), int(item.get("mes", 1) or 1)


def build_history_point(periodo: str | None, totals: dict) -> dict:
    month_year = extract_month_year(periodo or "")
    if month_year:
        year, month = month_year
    else:
        year, month = datetime.now().year, datetime.now().month
    full, short = format_period_label(year, month)
    return {
        "periodo": periodo or full,
        "label": short,
        "ano": year,
        "mes": month,
        "total": totals["total"],
        "brasileira": totals["brasileira"],
        "estrangeira": totals["estrangeira"],
    }


def merge_history(data: dict) -> list[dict]:
    history = {historico_key(item): dict(item) for item in OFFICIAL_HISTORY}

    latest_json = JSON_DIR / "abeam-latest.json"
    if latest_json.exists():
        try:
            existing = json.loads(latest_json.read_text(encoding="utf-8"))
            for item in existing.get("historico", []):
                history[historico_key(item)] = item
        except Exception as exc:
            log.warning("Falha ao ler histórico existente: %s", exc)

    new_point = build_history_point(data.get("periodo"), data["totais"])
    history[historico_key(new_point)] = new_point

    merged = sorted(history.values(), key=historico_key)
    for item in merged:
        if item["brasileira"] + item["estrangeira"] != item["total"]:
            raise ValueError(f"Histórico inconsistente em {item.get('periodo') or item.get('label')}")
    return merged


def validate_data(data: dict):
    totals = data["totais"]
    if totals["brasileira"] + totals["estrangeira"] != totals["total"]:
        raise ValueError("Totais gerais inconsistentes.")
    if sum(item["total"] for item in data["por_tipo"]) != totals["total"]:
        raise ValueError("Soma dos tipos não bate com o total da frota.")
    if sum(item["brasileira"] for item in data["por_tipo"]) != totals["brasileira"]:
        raise ValueError("Soma brasileira por tipo não bate com o total brasileiro.")
    if sum(item["estrangeira"] for item in data["por_tipo"]) != totals["estrangeira"]:
        raise ValueError("Soma estrangeira por tipo não bate com o total estrangeiro.")
    for item in data["por_tipo"]:
        if item["brasileira"] + item["estrangeira"] != item["total"]:
            raise ValueError(f"Tipo inconsistente: {item['tipo']}")
    if not data["top_empresas"]:
        raise ValueError("Top empresas vazio.")
    for item in data.get("historico", []):
        if item["brasileira"] + item["estrangeira"] != item["total"]:
            raise ValueError(f"Histórico inconsistente: {item.get('periodo')}")


def extract_data(pdf_path: Path) -> dict:
    log.info(f"Extraindo dados: {pdf_path.name}")
    pages = read_pages(pdf_path)
    period, totals = extract_period_and_totals(pages)
    por_tipo = parse_types(pages, totals)
    top_empresas, empresas_total = parse_companies(pages, totals)

    data = {
        "source": "ABEAM / Syndarma",
        "extracted": datetime.now().isoformat(),
        "periodo": period,
        "totais": totals,
        "por_tipo": por_tipo,
        "top_empresas": top_empresas,
        "empresas_total": empresas_total,
    }
    data["historico"] = merge_history(data)
    validate_data(data)
    log.info(
        "Extração concluída: %s · %s embarcações · %s tipos",
        data["periodo"],
        data["totais"]["total"],
        len(data["por_tipo"]),
    )
    return data


def save_json(data: dict, label: str) -> Path:
    slug = slugify(label)
    target = JSON_DIR / f"abeam-{slug}.json"
    latest = JSON_DIR / "abeam-latest.json"
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    target.write_text(payload, encoding="utf-8")
    latest.write_text(payload, encoding="utf-8")
    return latest


def copy_json_to_static(json_path: Path):
    dest = STATIC_DIR / "abeam-latest.json"
    shutil.copyfile(json_path, dest)
    log.info(f"JSON copiado para {dest}")


def update_wordpress(data: dict):
    if not all([WP_URL, WP_USER, WP_PASSWORD]):
        return
    page_id = os.getenv("WP_PAGE_ID", "")
    if not page_id:
        log.warning("WP_PAGE_ID não definido. Pulando atualização do WordPress.")
        return
    endpoint = f"{WP_URL}/wp-json/wp/v2/pages/{page_id}"
    payload = {
        "meta": {
            "abeam_data": json.dumps(data, ensure_ascii=False),
            "abeam_updated": data["extracted"],
        }
    }
    r = requests.post(endpoint, json=payload, auth=(WP_USER, WP_PASSWORD), timeout=30)
    r.raise_for_status()
    log.info("WordPress atualizado com sucesso.")


def notify(data: dict):
    if not WEBHOOK_URL:
        return
    msg = {
        "text": (
            f"VAPOZEIRO — relatório ABEAM atualizado\n"
            f"Período: {data['periodo']}\n"
            f"Total: {data['totais']['total']}\n"
            f"Brasileira: {data['totais']['brasileira']}\n"
            f"Estrangeira: {data['totais']['estrangeira']}"
        )
    }
    try:
        requests.post(WEBHOOK_URL, json=msg, timeout=10)
    except Exception as exc:
        log.warning(f"Falha ao enviar notificação: {exc}")


def process_pdf(pdf_path: Path, label: str):
    data = extract_data(pdf_path)
    latest = save_json(data, label)
    copy_json_to_static(latest)
    update_wordpress(data)
    notify(data)
    return data


def run_pipeline(input_pdf: str | None = None):
    if input_pdf:
        pdf_path = Path(input_pdf)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF não encontrado: {pdf_path}")
        label = pdf_path.stem
        process_pdf(pdf_path, label)
        return

    link = get_latest_pdf_link()
    if not link:
        return
    state = load_state()
    if not is_new(link, state):
        log.info("Sem novo relatório. Nada para atualizar.")
        return
    pdf_path = download_pdf(link["url"], link["label"])
    if not pdf_path:
        raise RuntimeError("Falha no download do PDF.")
    process_pdf(pdf_path, link["label"])
    save_state({"last_url": link["url"], "last_label": link["label"], "last_run": datetime.now().isoformat()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Executa uma vez e termina")
    parser.add_argument("--input-pdf", help="Usa um PDF local em vez de buscar na ABEAM")
    args = parser.parse_args()

    try:
        if args.once or args.input_pdf:
            run_pipeline(args.input_pdf)
            return
        run_pipeline()
        schedule.every().day.at("08:00").do(run_pipeline)
        while True:
            schedule.run_pending()
            time.sleep(60)
    except Exception as exc:
        log.exception("Falha no pipeline: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
