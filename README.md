# VAPOZEIRO — Pipeline de Automação ABEAM

Atualiza automaticamente o dashboard de frota offshore do site,
detectando e processando novos relatórios mensais da ABEAM.

## Como funciona

```
abeam.org.br  →  scraper.py  →  PDF  →  extrator  →  JSON  →  site
                                                          ↓
                                                    notificação
```

1. Roda todo dia às 08h e verifica se existe PDF novo na ABEAM
2. Se sim, baixa, extrai os dados e gera `abeam-latest.json`
3. Atualiza o site via WordPress API ou copia o JSON para a pasta pública
4. Dispara notificação via webhook (Slack, Discord, etc.)

## Setup rápido

### 1. Instalar dependências

```bash
pip install requests beautifulsoup4 pdfplumber schedule python-dotenv
```

### 2. Configurar variáveis

```bash
cp .env.example .env
# edite o .env com suas credenciais
```

### 3. Rodar

```bash
# Teste manual (roda uma vez e para)
python scraper.py --once

# Modo contínuo (roda + agenda para todo dia 08h)
python scraper.py
```

---

## Como consumir o JSON no site

O pipeline gera sempre um arquivo `public/data/abeam-latest.json` com esta estrutura:

```json
{
  "source": "ABEAM / Syndarma",
  "extracted": "2026-02-15T10:30:00",
  "periodo": "Fevereiro 2026",
  "totais": {
    "total": 481,
    "brasileira": 390,
    "estrangeira": 91,
    "pct_brasileira": 81
  },
  "por_tipo": [
    { "tipo": "PSV / OSRV", "total": 203, "brasileira": 181, "estrangeira": 22, "pct": 42.2 }
  ],
  "top_empresas": [
    { "empresa": "Bram Offshore", "total": 78, "brasileira": 67, "estrangeira": 11 }
  ]
}
```

### Exemplo: buscar no frontend (JS puro)

```javascript
fetch('/data/abeam-latest.json')
  .then(r => r.json())
  .then(data => {
    document.getElementById('total').textContent = data.totais.total;
    document.getElementById('periodo').textContent = data.periodo;
    // renderizar gráficos com Chart.js usando data.por_tipo etc.
  });
```

### WordPress (shortcode)

Se usar a integração WP, crie um shortcode que lê o campo `abeam_data` da página e injeta no frontend.

---

## Opções de deploy (onde rodar o script)

| Opção | Custo | Dificuldade | Recomendado para |
|---|---|---|---|
| **Railway / Render** | Grátis (free tier) | Baixa | Começar agora |
| **GitHub Actions** (cron) | Grátis | Baixa | Sem servidor |
| **VPS (DigitalOcean $6/mês)** | Baixo | Média | Mais controle |
| **Máquina local** | Zero | Zero | Teste apenas |

### Deploy no GitHub Actions (mais simples, sem servidor)

Crie `.github/workflows/abeam.yml`:

```yaml
name: ABEAM Pipeline
on:
  schedule:
    - cron: '0 11 * * *'   # todo dia às 08h BRT (11h UTC)
  workflow_dispatch:        # permite rodar manualmente

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install requests beautifulsoup4 pdfplumber python-dotenv
      - run: python scraper.py
        env:
          WP_URL:      ${{ secrets.WP_URL }}
          WP_USER:     ${{ secrets.WP_USER }}
          WP_PASSWORD: ${{ secrets.WP_PASSWORD }}
          WP_PAGE_ID:  ${{ secrets.WP_PAGE_ID }}
          WEBHOOK_URL: ${{ secrets.WEBHOOK_URL }}
      - uses: actions/upload-artifact@v4
        with:
          name: abeam-data
          path: data/json/
```

Configure os secrets em: GitHub repo → Settings → Secrets → Actions

---

## Estrutura de arquivos gerada

```
data/
├── pdfs/
│   └── abeam-fevereiro-2026.pdf
├── json/
│   ├── abeam-fevereiro-2026.json
│   └── abeam-latest.json          ← o site sempre consome este
└── last_processed.json            ← controla o que já foi processado

public/data/
└── abeam-latest.json              ← cópia pública (servida pelo site)
```

---

## Limitações e pontos de atenção

- **Extração de PDF**: `pdfplumber` funciona bem para texto, mas tabelas com layout complexo podem exigir ajuste fino no regex. Se um mês vier diferente, o log vai avisar.
- **Rate limiting**: o script tem delay entre requests e não faz scraping agressivo. A ABEAM é um site de associação sem proteção anti-bot, mas respeitar é importante.
- **Autenticação WP**: use sempre Application Passwords (WP 5.6+), nunca a senha principal.
- **Monitorar o log**: `pipeline.log` registra tudo. Se a extração falhar por mudança de layout, vai aparecer aqui.
