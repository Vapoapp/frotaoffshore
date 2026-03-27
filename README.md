# VAPOZEIRO — Pipeline ABEAM automático

Este pacote atualiza o dashboard da frota offshore automaticamente.

## O que foi corrigido

- extração real dos totais por tipo a partir das tabelas do PDF
- inclusão dos tipos menores: HLV, DLV, OTSV e DSV
- validação automática para bloquear JSON inconsistente
- soma por tipo agora fecha exatamente com a frota total
- `--once` para rodar no GitHub Actions sem travar
- `--input-pdf` para teste local com PDF já baixado
- cópia do `abeam-latest.json` para `public/data`

## Teste local

```bash
pip install -r requirements.txt
python scraper.py --once --input-pdf "Frota-de-Apoio-Maritimo-FEVEREIRO-2026-R.pdf"
```

## Produção

O workflow roda:

```bash
python scraper.py --once
```

Se houver PDF novo e os dados passarem na validação, o arquivo `public/data/abeam-latest.json` é atualizado e publicado.

Se não houver PDF novo, o site continua servindo o `abeam-latest.json` já versionado no repositório.
