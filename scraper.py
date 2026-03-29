import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

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
    "março": 3,
    "marco": 3,
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

MONTHS_ASCII = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
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

HISTORY_SEED = [
    {"periodo": "1985", "label": "1985", "ano": 1985, "mes": 1, "total": 108, "brasileira": 13, "estrangeira": 95},
    {"periodo": "1997", "label": "1997", "ano": 1997, "mes": 1, "total": 137, "brasileira": 32, "estrangeira": 105},
    {"periodo": "2004", "label": "2004", "ano": 2004, "mes": 1, "total": 155, "brasileira": 65, "estrangeira": 90},
    {"periodo": "2007", "label": "2007", "ano": 2007, "mes": 1, "total": 205, "brasileira": 112, "estrangeira": 93},
    {"periodo": "2014", "label": "2014", "ano": 2014, "mes": 1, "total": 500, "brasileira": 243, "estrangeira": 257},
    {"periodo": "2024", "label": "2024", "ano": 2024, "mes": 1, "total": 453, "brasileira": 382, "estrangeira": 71},
]


def normalize_text(text: str) -> str:
    cleaned = (text or "").strip().lower()
    replacements = str.maketrans({
        "á": "a", "à": "a", "ã": "a", "â": "a",
        "é": "e", "ê": "e",
        "í": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u",
        "ç": "c",
    })
    cleaned = cleaned.translate(replacements)
    return re.sub(r"\s+", " ", cleaned)


def month_year_from_text(text: str) -> Optional[tuple[int, int]]:
    normalized = normalize_text(text)

    for month_name, month in MONTHS_ASCII.items():
        match = re.search(rf"\b{month_name}\b.*?\b(19\d{{2}}|20\d{{2}})\b", normalized)
        if match:
            return int(match.group(1)), month
        match = re.search(rf"\b(19\d{{2}}|20\d{{2}})\b.*?\b{month_name}\b", normalized)
        if match:
            return int(match.group(1)), month

    match = re.search(r"\b(0?[1-9]|1[0-2])[\/-](19\d{2}|20\d{2})\b", normalized)
    if match:
        return int(match.group(2)), int(match.group(1))

    match = re.search(r"\b(19\d{2}|20\d{2})[\/-](0?[1-9]|1[0-2])\b", normalized)
    if match:
        return int(match.group(1)), int(match.group(2))

    return None


def extract_year_only(text: str) -> Optional[int]:
    normalized = normalize_text(text)
    match = re.search(r"\b(19\d{2}|20\d{2})\b", normalized)
    if not match:
        return None
    return int(match.group(1))


def build_link_rank(label: str, href: str) -> tuple[int, int, int, str]:
    combined = f"{label} {href}"
    parsed = month_year_from_text(combined)
    if parsed:
        year, month = parsed
        return year, 1, month, combined.lower()

    year = extract_year_only(combined)
    if year is not None:
        return year, 0, 0, combined.lower()

    return 0, 0, 0, combined.lower()


def period_to_label(periodo: str) -> str:
    parsed = month_year_from_text(periodo)
    if not parsed:
        return periodo
    year, month = parsed
    abbreviations = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    return f"{abbreviations[month - 1]}/{str(year)[2:]}"


def get_latest_pdf_link() -> Optional[dict]:
    log.info("Verificando página ABEAM...")
    try:
        r = requests.get(ABEAM_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Erro ao acessar ABEAM: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "wpdmdl" not in href:
            continue
        label = a.get_text(" ", strip=True)
        rank = build_link_rank(label, href)
        candidates.append({
            "label": label,
            "url": href,
            "rank": rank,
            "year": rank[0],
            "complete": bool(rank[1]),
            "month": rank[2] if rank[2] else None,
        })

    if not candidates:
        log.warning("Nenhum link de download encontrado.")
        return None

    candidates.sort(key=lambda item: item["rank"], reverse=True)
    latest = candidates[0]
    log.info(
        "PDF escolhido: %s | ano=%s mes=%s completo=%s",
        latest["label"],
        latest["year"],
        latest["month"],
        latest["complete"],
    )
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
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def download_pdf(url: str, label: str) -> Optional[Path]:
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


def extract_period_and_totals(pages: list[str]) -> tuple[Optional[str], dict]:
    text = "\n".join(pages)
    period_match = re.search(
        r"(Janeiro|Fevereiro|Março|Marco|Abril|Maio|Junho|Julho|Agosto|Setembro|Outubro|Novembro|Dezembro)\s*/?\s*(\d{4})",
        text,
        re.IGNORECASE,
    )
    period = f"{period_match.group(1).capitalize()} {period_match.group(2)}" if period_match else None

    totals_match = re.search(
        r"(\d+)\s+embarca[çc][õo]es,\s*(\d+)\s*\((\d+)%\)\s*de bandeira brasileira\s*e\s*(\d+)\s*\((\d+)%\)\s*de bandeira estrangeira",
        text,
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


def parse_total_line(page_text: str, expected_last: int, min_numbers: int) -> list[int]:
    for line in page_text.splitlines():
        if line.startswith("Total"):
            nums = ints_from_line(line)
            if nums and nums[-1] == expected_last and len(nums) >= min_numbers:
                return nums
    raise ValueError(f"Linha Total não encontrada para valor final {expected_last}.")


def find_page_with_total(pages: list[str], expected_last: int, min_numbers: int, required_terms: list[str]) -> str:
    for page_text in pages:
        page_lower = page_text.lower()
        if not all(term.lower() in page_lower for term in required_terms):
            continue
        try:
            parse_total_line(page_text, expected_last, min_numbers)
            return page_text
        except ValueError:
            continue
    raise ValueError(f"Página com linha Total {expected_last} não encontrada.")


def parse_types(pages: list[str], totals: dict) -> list[dict]:
    total_page = find_page_with_total(
        pages,
        totals["total"],
        16,
        ["psv", "ahts", "csv", "total"],
    )
    total_counts = parse_total_line(total_page, totals["total"], 16)[:-1]
    if len(total_counts) != len(TYPE_ORDER):
        raise ValueError("Contagem total por tipo não bate com o número esperado de colunas.")

    foreign_page = find_page_with_total(
        pages,
        totals["estrangeira"],
        12,
        ["plsv", "csv", "ahts", "total"],
    )
    foreign_counts = parse_total_line(foreign_page, totals["estrangeira"], 12)[:-1]
    if len(foreign_counts) != len(FOREIGN_TYPE_ORDER):
        raise ValueError("Contagem estrangeira por tipo não bate com o número esperado de colunas.")

    total_map = dict(zip(TYPE_ORDER, total_counts))
    foreign_map = {k: 0 for k in TYPE_ORDER}
    foreign_map.update(dict(zip(FOREIGN_TYPE_ORDER, foreign_counts)))

    result = []
    for vessel_type in TYPE_ORDER:
        total = total_map[vessel_type]
        foreign = foreign_map[vessel_type]
        brazilian = total - foreign
        if brazilian < 0:
            raise ValueError(f"Tipo {vessel_type} ficou com bandeira brasileira negativa.")
        result.append(
            {
                "tipo": vessel_type,
                "total": total,
                "brasileira": brazilian,
                "estrangeira": foreign,
                "pct": round(total / totals["total"] * 100, 1),
            }
        )
    return result


def normalize_status(status: str) -> str:
    cleaned = re.sub(r"\s+", " ", status.strip())
    if cleaned.lower() in {"nao associado", "não associado"}:
        return "Não Associado"
    return cleaned


def parse_company_line(line: str) -> Optional[dict]:
    if line.startswith("Total") or "Empresa Status Total" in line or line.startswith("Bandeira"):
        return None
    m = re.match(r"^(.*?)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s-]*?)\s+((?:\d+\s+){1,2}\d+)\s*$", line)
    if not m:
        return None
    empresa = m.group(1).strip()
    status = normalize_status(m.group(2))
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
    candidate_pages = [page for page in pages if "empresa" in page.lower() and "status" in page.lower()]
    for page_text in candidate_pages:
        companies = []
        for line in page_text.splitlines():
            row = parse_company_line(line)
            if row:
                companies.append(row)
        if companies and sum(c["total"] for c in companies) == totals["total"]:
            total_empresas = len(companies)
            top = sorted(companies, key=lambda x: (-x["total"], x["empresa"]))[:10]
            return top, total_empresas
    raise ValueError("Não foi possível extrair a tabela de empresas.")


def build_history_entry(periodo: str, totals: dict) -> dict:
    parsed = month_year_from_text(periodo)
    if not parsed:
        raise ValueError(f"Período inválido para histórico: {periodo}")
    year, month = parsed
    return {
        "periodo": periodo,
        "label": period_to_label(periodo),
        "ano": year,
        "mes": month,
        "total": totals["total"],
        "brasileira": totals["brasileira"],
        "estrangeira": totals["estrangeira"],
    }


def merge_history(data: dict, latest_json_path: Path) -> dict:
    history = list(HISTORY_SEED)
    if latest_json_path.exists():
        try:
            existing = json.loads(latest_json_path.read_text(encoding="utf-8"))
            existing_history = existing.get("historico", [])
            if isinstance(existing_history, list):
                history.extend(existing_history)
        except Exception as exc:
            log.warning(f"Falha ao ler histórico existente: {exc}")

    by_key = {}
    for item in history:
        periodo = item.get("periodo") or item.get("label")
        if periodo:
            by_key[periodo] = item

    if data.get("periodo"):
        current_entry = build_history_entry(data["periodo"], data["totais"])
        by_key[current_entry["periodo"]] = current_entry

    history_merged = sorted(by_key.values(), key=lambda item: (item.get("ano", 0), item.get("mes", 0), item.get("label", "")))
    data["historico"] = history_merged
    return data


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
    historico = data.get("historico", [])
    for item in historico:
        if item["brasileira"] + item["estrangeira"] != item["total"]:
            raise ValueError(f"Histórico inconsistente: {item.get('periodo', item.get('label', '?'))}")


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
    data = merge_history(data, latest)
    validate_data(data)
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


def run_pipeline(input_pdf: Optional[str] = None):
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
