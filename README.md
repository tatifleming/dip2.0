# Reprodutibilidade do corpus sobre mercado de carbono regulado

Repositório de código e dados de entrada para reproduzir a etapa de **obtenção, validação, organização e deduplicação de PDFs** de uma pesquisa bibliográfica sobre mercado de carbono.

O repositório reúne rotinas Python específicas para diferentes editoras, uma rotina geral de recuperação de PDFs diretamente disponíveis, uma rotina de deduplicação e o arquivo bibliográfico `artigos.csv` utilizado como entrada.

> **Princípio de acesso:** os scripts foram desenvolvidos para utilizar páginas oficiais, APIs autorizadas e arquivos diretamente disponíveis. Eles **não devem contornar paywalls, autenticação, CAPTCHA, bloqueios anti-bot ou outras restrições técnicas**. Quando uma verificação humana for apresentada no navegador, a resolução permanece manual.

---

## 1. Objetivo

O objetivo deste repositório é tornar a etapa computacional do levantamento bibliográfico **auditável e reprodutível**. Em particular, ele permite documentar:

1. qual arquivo de metadados foi usado como entrada;
2. quais scripts foram executados;
3. quais editoras e estratégias de obtenção foram utilizadas;
4. quais versões de software compunham o ambiente;
5. quais arquivos foram obtidos, rejeitados ou não encontrados;
6. como PDFs provenientes de rotas diferentes foram consolidados e deduplicados.

A reprodutibilidade de downloads na web tem uma limitação importante: disponibilidade de PDFs, assinaturas institucionais, URLs, páginas das editoras e mecanismos de acesso podem mudar. Portanto, a reprodução deve preservar **código, dados de entrada, versões, logs e critérios**, mesmo quando uma execução futura não conseguir obter exatamente os mesmos arquivos.

---

## 2. Origem do corpus bibliográfico

A busca bibliográrica foi realizada em **22 de abril de 2026** nas bases **Scopus** e **Web of Science (WoS)**.

### Scopus

```text
ALL("carbon market") AND (NOT REF("carbon market") OR TITLE-ABS-KEY("carbon market"))
```

### Web of Science

```text
TS("carbon market")
```

A busca consolidada resultou em **5.839 documentos sem duplicatas**, dos quais **4.282 foram identificados como artigos**. O arquivo `artigos.csv` disponível neste repositório contém esses **4.282 registros** e é utilizado como arquivo de entrada pelas rotinas de obtenção dos PDFs.


### Estrutura do CSV

O arquivo contém as colunas:

```text
Authors
Title
Year
DOI
Link
Cited by
Abstract
Document Type
Publisher
Open Access
```

No snapshot incluído neste repositório:

- registros: **4.282**;
- registros com título preenchido: **4.282**;
- registros com DOI preenchido: **4.282**;
- registros com link preenchido: **4.282**;
- valores distintos no campo `Publisher`: **459**;
- ocorrências adicionais de DOI repetido além da primeira: **11**.

Consulte [`docs/DADOS_E_PROVENIENCIA.md`](docs/DADOS_E_PROVENIENCIA.md) para detalhes e observações sobre redistribuição dos metadados.

---

## 3. Estrutura do repositório

```text
corpus-carbono-reprodutibilidade/
├── README.md
├── artigos.csv
├── requirements.txt
├── environment.yml
├── .gitignore
|
├── docs/
│   ├── DADOS_E_PROVENIENCIA.md
│   ├── REPRODUTIBILIDADE.md
│   └── SEGURANCA_E_ACESSO.md
|
└── scripts/
    ├── baixar_arquivos_geral.py
    ├── baixar_pdfs_elsevier.py
    ├── baixar_pdfs_mdpi_pipeline.py
    ├── baixar_pdfs_multiplas_editoras.py
    ├── baixar_pdfs_sessao_unica_taylor_e_francis.py
    ├── baixar_pdfs_springer.py
    └── deduplicar_pdfs.py
```

---

## 4. Software e versões de referência

### Sistema operacional

Os scripts são Python e grande parte do código é multiplataforma. Para as rotinas com Selenium, recomenda-se Windows 10/11, Linux ou macOS com navegador gráfico instalado.

### Python

Ambiente de referência usado na preparação e validação estática deste repositório:

```text
Python 3.13.5
```

Recomendação para reprodução:

```text
Python 3.13.x
```

### Bibliotecas Python

O arquivo `requirements.txt` fixa o ambiente de referência:

| Biblioteca | Versão |
|---|---:|
| pandas | 2.2.3 |
| requests | 2.32.5 |
| beautifulsoup4 | 4.14.3 |
| selenium | 4.35.0 |
| tqdm | 4.67.3 |
| openpyxl | 3.1.5 |
| PyMuPDF | 1.26.7 |
| urllib3 | 2.7.0 |

### Navegador

As rotinas que usam Selenium requerem um destes navegadores:

- **Google Chrome**, versão estável compatível com o Selenium instalado; ou
- **Microsoft Edge**, versão estável compatível com o Selenium instalado.

O `baixar_pdfs_mdpi_pipeline.py` usa Edge por padrão e também aceita Chrome. As demais rotinas Selenium usam Chrome.

Em Selenium 4, o Selenium Manager normalmente resolve o driver adequado automaticamente. Caso isso não seja possível no ambiente local, instale manualmente o driver correspondente à versão do navegador.


---

## 5. Instalação

### Opção A — `venv` + `pip` (recomendada)

Clone o repositório e entre na pasta:

```bash
git clone <URL-DO-REPOSITORIO>
cd corpus-carbono-reprodutibilidade
```

Crie o ambiente virtual:

```bash
python -m venv .venv
```

Ative-o no Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

No Linux/macOS:

```bash
source .venv/bin/activate
```

Atualize o `pip` e instale as dependências:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verifique a versão do Python:

```bash
python --version
```

### Opção B — Conda/Mamba

```bash
conda env create -f environment.yml
conda activate corpus-carbono
```

---

## 6. Visão geral dos programas

| Programa | Finalidade | Entrada principal | Resultado principal |
|---|---|---|---|
| `baixar_arquivos_geral.py` | Busca geral por PDFs diretamente acessíveis, sem depender de uma editora específica | CSV/XLSX | PDFs organizados por editora/ano, estado, logs e relatórios |
| `baixar_pdfs_elsevier.py` | Rotina específica para Elsevier, imprints e afiliadas reconhecidas | `artigos.csv` | `downloads_elsevier/` + `resultado_downloads.csv` |
| `baixar_pdfs_springer.py` | Rotina específica para Springer Nature e editoras associadas configuradas | `artigos.csv` | PDFs Springer + log CSV |
| `baixar_pdfs_mdpi_pipeline.py` | Pipeline MDPI em duas etapas: localizar links oficiais e baixar PDFs | `artigos.csv` | HTML/CSV de links, PDFs MDPI e log |
| `baixar_pdfs_sessao_unica_taylor_e_francis.py` | Sessão Selenium única para Wiley ou Taylor & Francis | `artigos.csv` | PDFs + log CSV |
| `baixar_pdfs_multiplas_editoras.py` | Rotina Selenium para Frontiers, SAGE, World Scientific e Emerald | `artigos.csv` | PDFs + log CSV |
| `deduplicar_pdfs.py` | Consolida PDFs e remove duplicatas, priorizando DOI | Diretório de PDFs | corpus deduplicado + relatórios CSV/JSON |

---

# 7. Uso detalhado

## 7.1 `baixar_arquivos_geral.py`

### Quando usar

Esta é a rotina mais abrangente. Ela recebe um arquivo CSV ou XLSX, tenta resolver DOI/links e localizar PDFs diretamente acessíveis. É útil como **primeira passagem geral** ou como complemento às rotinas específicas.

Ela inclui:

- leitura de CSV/XLSX;
- normalização de colunas;
- resolução de DOI;
- análise de HTML em busca de candidatos a PDF;
- validação por `Content-Type`, extensão e assinatura `%PDF-`;
- organização em `Publisher/Year/`;
- retomada por arquivo de estado JSON Lines;
- deduplicação operacional por DOI, URL final e caminho/nome;
- paralelismo controlado;
- relatórios CSV e/ou XLSX.

### Exemplo recomendado

```bash
python scripts/baixar_arquivos_geral.py \
  --input artigos.csv \
  --output downloads_geral \
  --workers 5 \
  --timeout 20 \
  --retries 3 \
  --report-format both \
  --log-level INFO
```

No Windows PowerShell, o comando pode ser colocado em uma única linha:

```powershell
python scripts/baixar_arquivos_geral.py --input artigos.csv --output downloads_geral --workers 5 --timeout 20 --retries 3 --report-format both --log-level INFO
```

### Parâmetros

| Parâmetro | Obrigatório | Padrão | Descrição |
|---|---|---:|---|
| `--input` | sim | — | CSV ou XLSX de entrada |
| `--output` | sim | — | diretório de saída |
| `--workers` | não | 5 | número de downloads em paralelo |
| `--timeout` | não | 20 | timeout de leitura HTTP, em segundos |
| `--retries` | não | 3 | tentativas para erros transitórios |
| `--backoff-base` | não | 1.5 | base do backoff exponencial |
| `--max-filename-length` | não | 140 | limite de caracteres do nome do arquivo |
| `--user-agent` | não | interno | User-Agent HTTP |
| `--insecure` | não | desativado | desabilita validação SSL; **não recomendado** |
| `--report-format` | não | `both` | `csv`, `xlsx` ou `both` |
| `--log-level` | não | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR` |

### Saídas

Dentro do diretório indicado por `--output`, a rotina cria estrutura para downloads, estado, logs e relatórios. O estado parcial é preservado para permitir retomada.

---

## 7.2 `baixar_pdfs_elsevier.py`

### Quando usar

Use para registros associados à Elsevier e aos imprints/afiliadas reconhecidos pelo script. A rotina utiliza DOI como identificador principal e pode consultar a API oficial Elsevier/ScienceDirect quando existirem credenciais autorizadas.

### Credenciais opcionais

O script reconhece as seguintes variáveis de ambiente:

```text
ELSEVIER_API_KEY
ELSEVIER_INSTTOKEN
ELSEVIER_ACCESS_TOKEN
```

Elas **não devem ser gravadas no repositório**.

Windows PowerShell:

```powershell
$env:ELSEVIER_API_KEY="SUA_CHAVE"
python scripts/baixar_pdfs_elsevier.py
```

Linux/macOS:

```bash
export ELSEVIER_API_KEY="SUA_CHAVE"
python scripts/baixar_pdfs_elsevier.py
```

### Entrada e saídas

Este script usa atualmente constantes internas:

```text
Entrada: artigos.csv
Pasta: downloads_elsevier/
Log: resultado_downloads.csv
```

Por isso, execute-o a partir da raiz do repositório:

```bash
python scripts/baixar_pdfs_elsevier.py
```

O script também tenta reaproveitar downloads bem-sucedidos registrados em um log anterior, se os arquivos ainda existirem.

---

## 7.3 `baixar_pdfs_springer.py`

### Quando usar

Filtra registros Springer/Springer Nature e nomes associados configurados no programa, resolve DOI e consulta páginas oficiais em `link.springer.com` e `nature.com`.

### Execução

```bash
python scripts/baixar_pdfs_springer.py \
  --csv artigos.csv \
  --out pdfs_springer \
  --log download_log_springer.csv \
  --delay 1.5
```

Windows PowerShell:

```powershell
python scripts/baixar_pdfs_springer.py --csv artigos.csv --out pdfs_springer --log download_log_springer.csv --delay 1.5
```

### Parâmetros principais

- `--csv`: CSV de entrada;
- `--out`: pasta para os PDFs;
- `--log`: log CSV;
- `--delay`: pausa entre artigos;
- `--force`: força uma nova tentativa mesmo quando já existe um PDF válido.

---

## 7.4 `baixar_pdfs_mdpi_pipeline.py`

### Quando usar

A rotina MDPI possui dois estágios integrados:

1. filtra os artigos MDPI e gera uma lista/HTML com links oficiais;
2. processa os links e tenta baixar os PDFs.

### Pipeline completo

```bash
python scripts/baixar_pdfs_mdpi_pipeline.py \
  --modo pipeline \
  --csv artigos.csv \
  --saida pdfs_mdpi \
  --log download_log_mdpi.csv \
  --browser edge \
  --modo-navegador oculto
```

### Somente gerar links

```bash
python scripts/baixar_pdfs_mdpi_pipeline.py \
  --modo gerar-html \
  --csv artigos.csv
```

### Somente baixar um HTML já gerado

```bash
python scripts/baixar_pdfs_mdpi_pipeline.py \
  --modo baixar-html \
  --html links_pendentes_mdpi.html \
  --saida pdfs_mdpi
```

### Navegador

Valores aceitos:

```text
--browser edge
--browser chrome
```

Modos:

```text
--modo-navegador oculto
--modo-navegador headless
--modo-navegador visivel
```

O modo `oculto` é o padrão. O código registra bloqueios ou falhas em vez de tentar contornar restrições.

### Outros parâmetros úteis

- `--limite N`: teste com apenas N itens;
- `--email`: e-mail opcional para User-Agent/Crossref;
- `--sem-crossref`: desabilita o fallback de metadados Crossref;
- `--timeout-pagina`;
- `--timeout-download`;
- `--pausa-artigo`;
- `--delay`;
- `--user-data-dir` e `--profile-directory`: permitem usar um perfil específico do navegador.

---

## 7.5 `baixar_pdfs_sessao_unica_taylor_e_francis.py`

### Função

Apesar do nome enfatizar Taylor & Francis, o código mantido suporta **Wiley** e **Taylor & Francis** por meio do argumento `--editora`.

Ele abre o Chrome uma vez, permite que o usuário resolva manualmente uma eventual verificação apresentada na primeira página e reutiliza a mesma sessão durante a sequência de artigos.

### Taylor & Francis

```bash
python scripts/baixar_pdfs_sessao_unica_taylor_e_francis.py \
  --csv artigos.csv \
  --editora taylor \
  --saida pdfs_taylor_francis \
  --log download_log_taylor.csv \
  --delay 2
```

### Wiley

```bash
python scripts/baixar_pdfs_sessao_unica_taylor_e_francis.py \
  --csv artigos.csv \
  --editora wiley \
  --saida pdfs_wiley \
  --log download_log_wiley.csv \
  --delay 2
```

### Teste com poucos artigos

```bash
python scripts/baixar_pdfs_sessao_unica_taylor_e_francis.py \
  --csv artigos.csv \
  --editora taylor \
  --saida teste_taylor \
  --log teste_taylor.csv \
  --limite 5
```

> O usuário deve resolver manualmente qualquer CAPTCHA/verificação. A automação não deve ser modificada para burlar a proteção.

---

## 7.6 `baixar_pdfs_multiplas_editoras.py`

### Editoras configuradas

A rotina detecta registros das seguintes plataformas:

- Frontiers;
- SAGE;
- World Scientific;
- Emerald.

Ela usa uma única sessão do Chrome e tenta aproveitar cookies ou um acesso já estabelecido na sessão, sem contornar restrições de acesso.

### Execução

```bash
python scripts/baixar_pdfs_multiplas_editoras.py \
  --csv artigos.csv \
  --saida pdfs_outras_editoras \
  --log download_log_outras_editoras.csv \
  --delay 0.5 \
  --timeout-pdf 15 \
  --page-wait 12 \
  --page-load-timeout 20
```

### Teste

```bash
python scripts/baixar_pdfs_multiplas_editoras.py \
  --csv artigos.csv \
  --saida teste_outras \
  --log teste_outras.csv \
  --limite 5
```

---

## 7.7 `deduplicar_pdfs.py`

### Por que esta etapa é necessária

Os mesmos artigos podem ser obtidos por mais de uma rota. Além disso, diferentes downloads podem gerar arquivos binariamente diferentes que representam o mesmo artigo. Por isso, a deduplicação deve ocorrer **depois da consolidação dos downloads**.

### Critérios usados

A rotina:

1. percorre recursivamente os PDFs da pasta de entrada;
2. extrai texto com PyMuPDF;
3. tenta extrair o DOI do conteúdo;
4. usa o DOI como principal chave de duplicidade;
5. para PDFs sem DOI, calcula hash do texto normalizado;
6. usa hash SHA-256 binário como fallback;
7. seleciona um arquivo principal por grupo;
8. copia apenas os arquivos mantidos para o corpus final;
9. gera relatórios de auditoria;
10. preserva separadamente os arquivos em que nenhum DOI foi identificado.

### Execução

```bash
python scripts/deduplicar_pdfs.py \
  --entrada downloads_brutos \
  --saida corpus_deduplicado \
  --relatorio relatorios_deduplicacao \
  --sem-doi sem_doi
```

No Windows PowerShell:

```powershell
python scripts/deduplicar_pdfs.py --entrada downloads_brutos --saida corpus_deduplicado --relatorio relatorios_deduplicacao --sem-doi sem_doi
```

Se `--sem-doi` não for informado, o programa utiliza a subpasta `_sem_doi` dentro da pasta de saída.

---

# 8. Fluxo recomendado para reproduzir o corpus

Uma execução organizada pode seguir esta sequência:

### Etapa 1 — confirmar entrada e ambiente

```bash
python --version
python -m pip freeze
```


### Etapa 2 — executar a rotina geral

```bash
python scripts/baixar_arquivos_geral.py --input artigos.csv --output downloads_geral --workers 5 --report-format both
```

### Etapa 3 — executar rotinas específicas

Exemplo de organização de saídas:

```text
resultados_execucao/
├── geral/
├── elsevier/
├── springer/
├── mdpi/
├── wiley/
├── taylor_francis/
└── outras_editoras/
```

Os nomes dos diretórios não são obrigatórios; o ponto essencial é preservar a origem de cada arquivo e os respectivos logs.

### Etapa 4 — reunir os PDFs em uma árvore de downloads brutos

Por exemplo:

```text
downloads_brutos/
├── geral/
├── elsevier/
├── springer/
├── mdpi/
├── wiley/
├── taylor_francis/
└── outras_editoras/
```

### Etapa 5 — deduplicar

```bash
python scripts/deduplicar_pdfs.py --entrada downloads_brutos --saida corpus_deduplicado --relatorio relatorios_deduplicacao --sem-doi sem_doi
```

---

# 9. Segurança e acesso responsável

Não versione:

- chaves de API;
- tokens institucionais;
- senhas;
- cookies;
- perfis autenticados de navegador;
- arquivos `.env` contendo segredos.

O `.gitignore` deste repositório exclui padrões comuns de credenciais e diretórios gerados.

Consulte [`docs/SEGURANCA_E_ACESSO.md`](docs/SEGURANCA_E_ACESSO.md).

---