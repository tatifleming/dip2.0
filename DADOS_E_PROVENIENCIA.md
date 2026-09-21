# Dados e proveniência

## Arquivo de entrada

O arquivo `artigos.csv` é o snapshot bibliográfico utilizado pelas rotinas deste repositório. Ele contém **4.282 registros** e as seguintes colunas:

- `Authors`
- `Title`
- `Year`
- `DOI`
- `Link`
- `Cited by`
- `Abstract`
- `Document Type`
- `Publisher`
- `Open Access`

No snapshot incluído no repositório, os 4.282 registros possuem `Title`, `DOI` e `Link` preenchidos. Foram observadas 459 representações distintas no campo `Publisher` e 11 ocorrências adicionais de DOI repetido além da primeira ocorrência. Essas repetições no nível de metadados são uma das razões para manter uma etapa posterior de deduplicação dos PDFs efetivamente obtidos.

## Origem da busca

A construção do corpus bibliográfico foi baseada em buscas na **Scopus** e na **Web of Science (WoS)**, realizadas em **22 de abril de 2026**. O arquivo `artigos.csv` corresponde ao subconjunto de artigos utilizado como entrada para as rotinas de obtenção de texto integral.

Equações registradas na nota técnica:

### Scopus

```text
ALL("carbon market") AND (NOT REF("carbon market") OR TITLE-ABS-KEY("carbon market"))
```

### Web of Science

```text
TS("carbon market")
```

Ao todo foram encontrados 5.839 documentos na busca consolidada sem duplicatas e 4.282 artigos identificados. O CSV deste repositório possui exatamente 4.282 linhas de dados, além do cabeçalho.
