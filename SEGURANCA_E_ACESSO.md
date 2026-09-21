# Segurança, acesso e credenciais

As rotinas foram organizadas para trabalhar com páginas oficiais, APIs autorizadas e documentos diretamente disponíveis. Elas não devem ser usadas para contornar paywalls, autenticação, CAPTCHA, bloqueios anti-bot ou outras restrições técnicas.

## Elsevier

O script `baixar_pdfs_elsevier.py` pode utilizar credenciais oficiais da API Elsevier quando elas forem disponibilizadas por variáveis de ambiente:

```text
ELSEVIER_API_KEY
ELSEVIER_INSTTOKEN
ELSEVIER_ACCESS_TOKEN
```

Nunca salve essas credenciais no código, no CSV, em arquivos versionados ou em commits do Git. O `.gitignore` exclui arquivos `.env`, mas a recomendação é usar variáveis de ambiente do sistema ou o mecanismo de Secrets do GitHub para automações autorizadas.

PowerShell:

```powershell
$env:ELSEVIER_API_KEY="SUA_CHAVE"
```

Linux/macOS:

```bash
export ELSEVIER_API_KEY="SUA_CHAVE"
```

## Selenium e CAPTCHA

Algumas rotinas usam navegador controlado pelo Selenium. Quando uma página solicitar verificação humana, a resolução deve permanecer manual. O código não deve ser alterado para automatizar ou contornar essas verificações.
