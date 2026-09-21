# Procedimento recomendado para reprodução

Este documento descreve uma sequência operacional que mantém separados os dados de entrada, os downloads brutos, os logs e o corpus final deduplicado.

## 1. Registrar a versão do código e dos dados

```bash
git rev-parse HEAD
sha256sum artigos.csv
```

No Windows, use `Get-FileHash` para o CSV.

## 2. Criar o ambiente Python

Use o `requirements.txt` ou `environment.yml` da raiz do repositório. O ambiente de referência foi preparado para Python 3.13.x.

## 3. Executar a busca geral por PDFs diretamente disponíveis

```bash
python scripts/baixar_arquivos_geral.py \
  --input artigos.csv \
  --output downloads_geral \
  --workers 5 \
  --report-format both
```

Essa rotina deve ser entendida como uma tentativa geral de recuperar apenas PDFs diretamente acessíveis. Ela não substitui as rotinas específicas de editoras, que conhecem estruturas de URL e fluxos próprios.

## 4. Executar as rotinas específicas

Execute somente as rotinas relevantes ao desenho da reprodução. Use pastas de saída separadas para preservar a origem de cada arquivo.

Exemplos completos estão no `README.md`.

## 5. Consolidar downloads brutos

Crie uma pasta de consolidação e copie para ela os PDFs obtidos pelas diferentes rotinas, preservando subpastas se desejar rastrear a origem:

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

## 6. Deduplicar o corpus

```bash
python scripts/deduplicar_pdfs.py \
  --entrada downloads_brutos \
  --saida corpus_deduplicado \
  --relatorio relatorios_deduplicacao \
  --sem-doi sem_doi
```

A rotina usa DOI como critério principal. Para arquivos sem DOI identificado, utiliza hashes de texto normalizado e, quando necessário, hash binário do arquivo. Os relatórios de auditoria devem ser preservados com os resultados da pesquisa.

## 7. Preservar evidências da execução

Para cada reprodução, guarde:

- hash do `artigos.csv`;
- hash/commit do repositório;
- versão do Python;
- `pip freeze` do ambiente;
- versão do Chrome ou Edge quando Selenium for utilizado;
- logs produzidos por cada script;
- relatórios de download;
- relatórios da deduplicação;
- data e horário da execução;
- informação sobre acesso institucional utilizado, sem armazenar credenciais.

Exemplo:

```bash
python --version > ambiente_python.txt
python -m pip freeze > ambiente_pip_freeze.txt
```

## 8. Limitações de reprodução

Resultados de download podem mudar ao longo do tempo porque disponibilidade de acesso aberto, assinaturas institucionais, URLs, políticas das editoras, mecanismos anti-bot e conteúdo das páginas mudam. Assim, reprodutibilidade neste fluxo significa preservar **entradas, código, versões, logs e critérios de seleção**, e não pressupõe que toda execução futura obtenha exatamente o mesmo conjunto de PDFs da internet.
