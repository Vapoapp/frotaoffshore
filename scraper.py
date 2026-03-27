from __future__ import annotations

"""
VAPOZEIRO — Pipeline automático ABEAM

Princípios:
- roda sem intervenção humana
- só publica JSON novo se a extração passar na validação
- se falhar, mantém o último JSON válido no site
- nunca usa fallback silencioso com números fixos fingindo serem novos
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber
import requests
try:
    import schedule
except Exception:  # pragma: no cover
    schedule = None
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("pipeline.log"), logging.StreamHandler()],
)
log = logging.getLogger("abeam")

ABEAM_URL = "https://abeam.org.br/estudo-da-frota/"
USER_AGENT = "Mozilla/5.0 (compatible; VapozeiroABEAM/1.0)"
DATA_DIR = Path("data")
PDF_DIR = DATA_DIR / "pdfs"
JSON_DIR = DATA_DIR / "json"
STATE_FILE = DATA_DIR / "last_processed.json"
SEED_JSON = Path("abeam-latest.json")

STATIC_DIR = Path(os.getenv("STATIC_DIR", "public/data"))
WP_URL = os.getenv("WP_URL", "")
WP_USER = os.getenv("WP_USER", "")
WP_PASSWORD = os.getenv("WP_PASSWORD", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

for d in [DATA_DIR, PDF_DIR, JSON_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

MONTH_RE = re.compile(
    r"(Janeiro|Fevereiro|Março|Abril|Maio|Junho|Julho|Agosto|Setembro|Outubro|Novembro|Dezembro)\s*/?\s*(\d{4})",
    re.IGNORECASE,
)

TYPE_TOTAL_ORDER = [
    "PSV / OSRV",
    "AHTS",
    "LH / SV",
    "RSV",
    "CSV/MPSV",
    "PLSV",
    "CREW / FSV",
    "FLOTEL/CSOV",
    "SDSV",
    "RV",
    "WSV",
    "HLV",
    "DLV",
    "OTSV",
    "DSV",
]

TYPE_BR_ORDER = [
    "PSV / OSRV",
    "LH / SV",
    "AHTS",
    "RSV",
    "CREW / FSV",
    "SDSV",
    "CSV/MPSV",
    "PLSV",
    "WSV",
    "RV",
    "OTSV",
    "DSV",
    "FLOTEL/CSOV",
]

TYPE_EX_ORDER = [
    "PSV / OSRV",
    "PLSV",
    "CSV/MPSV",
    "FLOTEL/CSOV",
    "AHTS",
    "RV",
    "HLV",
    "DLV",
    "RSV",
    "WSV",
    "CREW / FSV",
]

@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str]
    warnings: list[str]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_lines(text: str) -> list[str]:
    return [normalize_spaces(line) for line in text.splitlines() if normalize_spaces(line)]


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Falha ao ler %s: %s", path, exc)
    return None


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict[str, Any]:
    return load_json(STATE_FILE) or {}


def save_state(state: dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def get_latest_pdf_link() -> dict[str, str] | None:
    log.info("Verificando página ABEAM...")
    try:
        resp = requests.get(ABEAM_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Erro ao acessar ABEAM: %s", exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    links = [a for a in soup.find_all("a", href=True) if "wpdmdl" in a["href"]]
    if not links:
        log.error("Nenhum link de PDF encontrado na página da ABEAM.")
        return None

    latest = links[-1]
    return {"label": latest.get_text(strip=True), "url": latest["href"]}


def download_pdf(url: str, label: str) -> Path | None:
    out = PDF_DIR / f"abeam-{slugify(label)}.pdf"
    if out.exists() and out.stat().st_size > 0:
        log.info("PDF já existe: %s", out.name)
        return out

    log.info("Baixando PDF: %s", url)
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=90)
        resp.raise_for_status()
        out.write_bytes(resp.content)
        log.info("PDF salvo: %s (%s KB)", out.name, len(resp.content) // 1024)
        return out
    except requests.RequestException as exc:
        log.error("Erro no download do PDF: %s", exc)
        return None


def read_pdf_pages(pdf_path: Path) -> list[str]:
    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def extract_period(full_text: str) -> str | None:
    m = MONTH_RE.search(full_text)
    if not m:
        return None
    return f"{m.group(1).capitalize()} {m.group(2)}"


def extract_totals_overview(full_text: str) -> dict[str, int] | None:
    m = re.search(
        r"(\d{3,})\s+embarca[çc][õo]es.*?(\d{2,3})\s*\((\d+)%\)\s*de bandeira brasileira.*?(\d{2,3})\s*\((\d+)%\)\s*de bandeira estrangeira",
        full_text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    return {
        "total": int(m.group(1)),
        "brasileira": int(m.group(2)),
        "pct_brasileira": int(m.group(3)),
        "estrangeira": int(m.group(4)),
    }


def extract_total_line(page_text: str) -> list[int] | None:
    for line in split_lines(page_text):
        if line.startswith("Total "):
            nums = [int(n) for n in re.findall(r"\d+", line)]
            if nums:
                return nums
    return None


def parse_type_counts(page_total_text: str, page_br_text: str, page_ex_text: str, totals: dict[str, int]) -> list[dict[str, Any]]:
    total_nums = extract_total_line(page_total_text)
    br_nums = extract_total_line(page_br_text)
    ex_nums = extract_total_line(page_ex_text)

    if not total_nums or not br_nums or not ex_nums:
        raise ValueError("Não foi possível localizar as linhas Total das tabelas de tipos.")

    if len(total_nums) != len(TYPE_TOTAL_ORDER) + 1:
        raise ValueError(f"Tabela total por tipo inesperada: {len(total_nums)} números.")
    if len(br_nums) != len(TYPE_BR_ORDER) + 1:
        raise ValueError(f"Tabela brasileira por tipo inesperada: {len(br_nums)} números.")
    if len(ex_nums) != len(TYPE_EX_ORDER) + 1:
        raise ValueError(f"Tabela estrangeira por tipo inesperada: {len(ex_nums)} números.")

    total_map = dict(zip(TYPE_TOTAL_ORDER, total_nums[:-1]))
    br_map = dict(zip(TYPE_BR_ORDER, br_nums[:-1]))
    ex_map = dict(zip(TYPE_EX_ORDER, ex_nums[:-1]))

    results: list[dict[str, Any]] = []
    total_fleet = totals["total"]
    for tipo in TYPE_TOTAL_ORDER:
        br = br_map.get(tipo, 0)
        ex = ex_map.get(tipo, 0)
        total = total_map[tipo]
        results.append(
            {
                "tipo": tipo,
                "total": total,
                "brasileira": br,
                "estrangeira": ex,
                "pct": round((total / total_fleet) * 100, 1),
            }
        )

    results.sort(key=lambda x: (-x["total"], x["tipo"]))
    return results


def parse_company_table(page_text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in split_lines(page_text):
        if line.startswith(("Tabela ", "Bandeira", "Empresa ", "Brasileira ", "Frota ", "ABEAM –", "Total ")):
            continue
        if " ABEAM " in line or " Não Associado " in line:
            status = "ABEAM" if " ABEAM " in line else "Não Associado"
            name, rest = line.split(f" {status} ", 1)
            nums = [int(n) for n in re.findall(r"\d+", rest)]
            if not nums:
                continue
            if len(nums) == 1:
                br, ex, total = nums[0], 0, nums[0]
            elif len(nums) == 2:
                br, ex, total = nums[0], 0, nums[1]
            else:
                br, ex, total = nums[0], nums[1], nums[2]
            entries.append(
                {
                    "empresa": name.title().replace("Dof / Norskan", "DOF / Norskan").replace("Wsut", "WSUT").replace("Cbo", "CBO"),
                    "status": status,
                    "brasileira": br,
                    "estrangeira": ex,
                    "total": total,
                }
            )
    # remove possíveis duplicatas por empresa preservando a primeira ocorrência
    unique: dict[str, dict[str, Any]] = {}
    for item in entries:
        unique.setdefault(item["empresa"], item)
    return list(unique.values())


def parse_top_companies(page_desc_text: str, limit: int = 10) -> tuple[list[dict[str, Any]], int]:
    companies = parse_company_table(page_desc_text)
    companies.sort(key=lambda x: (-x["total"], x["empresa"]))
    top = [
        {
            "empresa": c["empresa"],
            "total": c["total"],
            "brasileira": c["brasileira"],
            "estrangeira": c["estrangeira"],
        }
        for c in companies[:limit]
    ]
    return top, len(companies)


def validate_data(data: dict[str, Any], previous: dict[str, Any] | None = None) -> ValidationResult:
    reasons: list[str] = []
    warnings: list[str] = []

    periodo = data.get("periodo")
    totais = data.get("totais", {})
    por_tipo = data.get("por_tipo", [])
    top_empresas = data.get("top_empresas", [])

    if not periodo:
        reasons.append("Período não identificado no PDF.")

    total = totais.get("total")
    br = totais.get("brasileira")
    ex = totais.get("estrangeira")
    pct_br = totais.get("pct_brasileira")

    if not all(isinstance(x, int) for x in [total, br, ex, pct_br]):
        reasons.append("Totais gerais incompletos.")
    else:
        if br + ex != total:
            reasons.append("Brasileira + estrangeira não bate com o total geral.")
        if pct_br != round(br / total * 100):
            warnings.append("Percentual de bandeira brasileira divergente do arredondamento esperado.")
        if total < 300 or total > 700:
            reasons.append("Total geral fora da faixa plausível para a frota.")

    if not por_tipo:
        reasons.append("Lista por tipo vazia.")
    else:
        sum_total = sum(int(item.get("total", 0)) for item in por_tipo)
        sum_br = sum(int(item.get("brasileira", 0)) for item in por_tipo)
        sum_ex = sum(int(item.get("estrangeira", 0)) for item in por_tipo)
        if total is not None and sum_total != total:
            reasons.append(f"Soma por tipo ({sum_total}) não bate com total geral ({total}).")
        if br is not None and sum_br != br:
            reasons.append(f"Soma brasileira por tipo ({sum_br}) não bate com total brasileiro ({br}).")
        if ex is not None and sum_ex != ex:
            reasons.append(f"Soma estrangeira por tipo ({sum_ex}) não bate com total estrangeiro ({ex}).")
        if len(por_tipo) < 10:
            reasons.append("Quantidade de tipos extraídos menor que o esperado.")

    if not top_empresas or len(top_empresas) < 5:
        reasons.append("Top empresas insuficiente.")
    else:
        last = None
        for item in top_empresas:
            if item["brasileira"] + item["estrangeira"] != item["total"]:
                reasons.append(f"Empresa {item['empresa']} com total inconsistente.")
                break
            if last is not None and item["total"] > last:
                reasons.append("Top empresas não está em ordem decrescente.")
                break
            last = item["total"]

    if previous:
        prev_periodo = previous.get("periodo")
        if prev_periodo == periodo:
            warnings.append("Período igual ao JSON anterior.")
        prev_total = previous.get("totais", {}).get("total")
        if isinstance(prev_total, int) and isinstance(total, int) and abs(prev_total - total) > 120:
            warnings.append("Variação muito grande no total geral versus último JSON válido.")

    return ValidationResult(ok=not reasons, reasons=reasons, warnings=warnings)


def extract_data(pdf_path: Path, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    log.info("Extraindo dados: %s", pdf_path.name)
    pages = read_pdf_pages(pdf_path)
    full_text = "\n".join(pages)

    periodo = extract_period(full_text)
    totals = extract_totals_overview(full_text)
    if not totals:
        raise ValueError("Não foi possível extrair os totais gerais do relatório.")

    if len(pages) < 16:
        raise ValueError("PDF com menos páginas do que o esperado para o parser atual.")

    por_tipo = parse_type_counts(pages[9], pages[12], pages[14], totals)
    top_empresas, empresas_total = parse_top_companies(pages[6], limit=10)

    data: dict[str, Any] = {
        "source": "ABEAM / Syndarma",
        "extracted": datetime.now().isoformat(),
        "periodo": periodo,
        "totais": totals,
        "por_tipo": por_tipo,
        "top_empresas": top_empresas,
        "empresas_total": empresas_total,
        "validation": {
            "status": "pending",
            "warnings": [],
            "errors": [],
        },
    }

    validation = validate_data(data, previous)
    data["validation"] = {
        "status": "validated" if validation.ok else "rejected",
        "warnings": validation.warnings,
        "errors": validation.reasons,
    }
    return data


def save_candidate_json(data: dict[str, Any], label: str) -> Path:
    slug = slugify(label)
    dated = JSON_DIR / f"abeam-{slug}.json"
    save_json(dated, data)
    save_json(JSON_DIR / "abeam-candidate.json", data)
    log.info("JSON candidato salvo: %s", dated.name)
    return dated


def publish_valid_json(data: dict[str, Any], label: str) -> Path:
    slug = slugify(label)
    dated = JSON_DIR / f"abeam-{slug}.json"
    latest = JSON_DIR / "abeam-latest.json"
    save_json(dated, data)
    save_json(latest, data)
    save_json(SEED_JSON, data)
    copy_json_to_static(latest)
    update_wordpress(data)
    log.info("JSON validado publicado: %s + abeam-latest.json", dated.name)
    return latest


def copy_json_to_static(source_json: Path) -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = STATIC_DIR / "abeam-latest.json"
    dest.write_text(source_json.read_text(encoding="utf-8"), encoding="utf-8")
    log.info("JSON copiado para pasta pública: %s", dest)


def update_wordpress(data: dict[str, Any]) -> None:
    if not all([WP_URL, WP_USER, WP_PASSWORD]):
        log.info("WordPress não configurado — pulando atualização WP.")
        return

    page_id = os.getenv("WP_PAGE_ID", "")
    if not page_id:
        log.warning("WP_PAGE_ID não definido — pulando atualização WP.")
        return

    endpoint = f"{WP_URL.rstrip('/')}/wp-json/wp/v2/pages/{page_id}"
    payload = {
        "meta": {
            "abeam_data": json.dumps(data, ensure_ascii=False),
            "abeam_updated": data["extracted"],
        }
    }
    try:
        resp = requests.post(endpoint, json=payload, auth=(WP_USER, WP_PASSWORD), timeout=30)
        resp.raise_for_status()
        log.info("WordPress atualizado com sucesso.")
    except requests.RequestException as exc:
        log.error("Erro ao atualizar WordPress: %s", exc)


def notify(message: str) -> None:
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"text": message}, timeout=10)
        log.info("Notificação enviada.")
    except Exception as exc:
        log.warning("Falha ao enviar notificação: %s", exc)


def ensure_seed_latest_available() -> bool:
    latest = JSON_DIR / "abeam-latest.json"
    if latest.exists():
        return True
    if SEED_JSON.exists():
        save_json(latest, json.loads(SEED_JSON.read_text(encoding="utf-8")))
        log.info("JSON seed copiado para data/json/abeam-latest.json")
        return True
    return False


def run_pipeline() -> int:
    log.info("=" * 60)
    log.info("Iniciando pipeline automático VAPOZEIRO / ABEAM")

    ensure_seed_latest_available()
    previous = load_json(JSON_DIR / "abeam-latest.json") or load_json(SEED_JSON)

    link = get_latest_pdf_link()
    if not link:
        notify("VAPOZEIRO / ABEAM: falha ao consultar a página da ABEAM.")
        return 1

    state = load_state()
    if link["url"] == state.get("last_url"):
        log.info("Sem PDF novo. Mantendo último JSON válido publicado.")
        if previous:
            save_json(JSON_DIR / "abeam-latest.json", previous)
        return 0

    pdf_path = download_pdf(link["url"], link["label"])
    if not pdf_path:
        notify(f"VAPOZEIRO / ABEAM: falha ao baixar novo relatório {link['label']}.")
        return 1

    try:
        data = extract_data(pdf_path, previous)
        save_candidate_json(data, link["label"])
    except Exception as exc:
        log.exception("Falha na extração do novo PDF")
        notify(f"VAPOZEIRO / ABEAM: erro de extração no relatório {link['label']}: {exc}")
        return 1

    validation = data["validation"]
    if validation["status"] != "validated":
        msg = (
            f"VAPOZEIRO / ABEAM: relatório {data.get('periodo') or link['label']} rejeitado pela validação automática. "
            f"Erros: {' | '.join(validation['errors'])}"
        )
        log.error(msg)
        notify(msg)
        return 1

    latest = publish_valid_json(data, link["label"])
    save_state(
        {
            "last_url": link["url"],
            "last_label": link["label"],
            "last_periodo": data["periodo"],
            "last_run": datetime.now().isoformat(),
            "last_json": str(latest),
        }
    )

    warning_text = ""
    if validation["warnings"]:
        warning_text = "\nAvisos: " + " | ".join(validation["warnings"])
    notify(
        f"VAPOZEIRO / ABEAM: relatório {data['periodo']} publicado automaticamente. "
        f"Total: {data['totais']['total']} | BR: {data['totais']['brasileira']} | EX: {data['totais']['estrangeira']}"
        f"{warning_text}"
    )
    log.info("Pipeline concluído com sucesso.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Executa uma vez e encerra.")
    args = parser.parse_args()

    once = args.once or os.getenv("RUN_ONCE", "false").lower() == "true"
    if once:
        return run_pipeline()

    code = run_pipeline()
    if schedule is None:
        log.error("Biblioteca schedule não instalada. Use --once ou instale as dependências.")
        return code
    schedule.every().day.at("08:00").do(run_pipeline)
    log.info("Agendador ativo — verificando diariamente às 08:00")
    while True:
        schedule.run_pending()
        time.sleep(60)
    return code


if __name__ == "__main__":
    sys.exit(main())
