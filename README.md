# VAPOZEIRO — Pipeline Automático ABEAM

Este pacote deixa o radar da frota rodando sozinho, com uma regra simples:

**só publica JSON novo se a extração passar na validação automática.**

Se a ABEAM mudar o layout e a extração falhar, o site continua usando o último JSON válido.

## Fluxo real

```text
ABEAM → scraper.py → PDF → extração → validação automática → publish JSON → deploy do site
                                           └──────── se falhar, NÃO publica o novo JSON
```

## O que este pipeline faz

1. verifica a página da ABEAM todo dia às 08h BRT;
2. detecta PDF novo;
3. baixa o PDF;
4. extrai período, totais, distribuição por tipo e top empresas;
5. valida os números automaticamente;
6. publica somente se o JSON novo estiver consistente;
7. mantém o último JSON válido se houver erro;
8. envia notificação opcional por webhook.

## O que mudou nesta versão

- remove fallback silencioso com números fixos fingindo ser mês novo;
- extrai os tipos a partir das tabelas do PDF;
- extrai o top de empresas a partir da tabela por empresa;
- cria `validation.status`, `validation.errors` e `validation.warnings` no JSON;
- bloqueia publicação quando a validação falha;
- preserva o último JSON válido no deploy;
- grava um JSON seed no repositório (`abeam-latest.json`) para bootstrap do site.

## Requisitos

```bash
pip install -r requirements.txt
```

## Estrutura esperada

```text
data/
├── pdfs/
├── json/
│   ├── abeam-candidate.json
│   ├── abeam-latest.json
│   └── abeam-<periodo>.json
└── last_processed.json

public/data/
└── abeam-latest.json
```

## Como rodar localmente

### Rodar uma vez

```bash
python scraper.py --once
```

### Rodar em modo agendado

```bash
python scraper.py
```

## Variáveis opcionais

```env
STATIC_DIR=public/data
WEBHOOK_URL=
WP_URL=
WP_USER=
WP_PASSWORD=
WP_PAGE_ID=
```

## Regras de validação automática

O pipeline só considera o JSON válido quando, no mínimo:

- `brasileira + estrangeira == total`
- soma de `por_tipo.total == total`
- soma de `por_tipo.brasileira == totais.brasileira`
- soma de `por_tipo.estrangeira == totais.estrangeira`
- top empresas não vem vazio
- os totais ficam dentro de uma faixa plausível

Se qualquer uma dessas regras falhar, o novo JSON é rejeitado e o site continua no último dado bom.

## Comportamento do GitHub Actions

O workflow:

- roda o scraper com `--once`
- publica artefatos do pipeline
- prepara a pasta `public`
- faz deploy no GitHub Pages com `keep_files: true`
- usa `data/json/abeam-latest.json` se existir
- se não existir, usa o `abeam-latest.json` versionado no repositório

## Observação importante

Este pipeline ficou **automático e seguro para publicação**, mas não “mágico”.

Se a ABEAM mudar radicalmente a estrutura das tabelas, o comportamento correto será:

- a validação falhar
- o JSON novo não ser publicado
- o site continuar com o último JSON válido

Isso é intencional.
