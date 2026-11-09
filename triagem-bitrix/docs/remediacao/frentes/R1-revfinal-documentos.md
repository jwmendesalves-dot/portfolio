# R1 — revisão final foco 1 (documentos): A1/A2/A3

Status: CONCLUÍDO. Esta frente continua uma anterior que foi interrompida: a primeira rodada deixou o código pronto e esta revisou o diff, que não precisou de ajuste, e escreveu os testes.
Fonte dos achados: `docs/remediacao/revisao/final-1-documentos.md`. Não houve git de escrita. Nenhum CPF existente foi alterado.

## Mudanças

- **A1 (`completar_cpf_por_nome.py`)**
  - `contatos_por_telefone` passa a fazer UMA chamada `crm.duplicate.findbycomm` por telefone, com os valores `t` e `+55t`, e grava no cache apenas os contatos daquele telefone. O limite é `MAX_TELEFONES_CONSULTA = 10` por lote.
  - Criado `VERSAO_CACHE = 2`. Um cache de versão menor que 2 perde a chave `fones` ao abrir: sai um log de aviso e o cache é regravado já na v2. Assim o cache contaminado que está em produção se limpa sozinho no deploy, sem passo manual.
- **A2 (`ponte_criterio.py`)**
  - `kit_completo` devolve `False` quando algum `file_id` documental (`lote_documental(lote)["file_ids"]`) não tem meta em `arquivos`.
  - Com isso `situacao_kit` não devolve mais `repetido` quando falta registro de algum documento.
- **A3 (`download_svc.py`)**
  - `_lote_destino` usa o `lote_id` do item só se esse lote existe e contém o fid. Caso contrário, escolhe um lote que contenha o fid, dando preferência ao sub-lote em vez do pai DIVIDIDO. O `indice` é recalculado a partir do lote escolhido.
  - Quando nenhum lote do estado tem o fid, o estado está corrompido ou o lote some antes da gravação (`<lote inexistente>`), o item da fila recebe `estado_pendente=True` com o meta guardado em `estado_meta`, e sai log de erro. A marca é limpa quando o meta for aplicado.
  - `processar_fila` reaplica os itens marcados como pendentes em toda rodada.
  - `_normalizar` não poda item `baixado` que ainda tenha `estado_pendente`.

## Testes (`testes/test_r1_revfinal_documentos.py`, 7 testes)

- A1: o cache não promove homônimo a `confirmar`, porque um lote só com o telefone de outra pessoa termina em `sugerir`.
- A1: uma consulta por telefone, e o cache de cada telefone guarda só os contatos dele.
- A1: um cache v1 perde `fones` ao abrir.
- A2: `file_id` sem meta não conta como kit completo nem como `repetido`; com o kit inteiro, volta a contar como completo.
- A3: item com `lote_id` velho é aplicado no lote que contém o fid.
- A3: em lote pai DIVIDIDO, o meta cai no sub-lote.
- A3: sem nenhum lote, o item fica `estado_pendente`; quando o lote aparece, a próxima aplicação grava o meta e limpa a marca.

Prova contra HEAD: o repo foi copiado para `$TMPDIR/r1head` e os 3 arquivos foram substituídos por `git show HEAD:<arq>`. Resultado: **7 failed**.
- Os testes de A1 e A2 falham na própria asserção, por exemplo `assert {'1', '2'} == {'2'}`, uma chamada com 4 valores, `versao 1 >= 2` e `True is False`.
- Os de A3 falham também porque em HEAD não existe `fila=` nem `_pendentes_de_estado`. O cenário central de A3 (o meta não chegar ao lote certo) já tinha sido reproduzido pelo revisor.

## Saídas

```
$ .venv/bin/python -m pyflakes completar_cpf_por_nome.py ponte_criterio.py download_svc.py testes/test_r1_revfinal_documentos.py
pyflakes exit=0

$ .venv/bin/python -m pytest -q testes/test_r1_revfinal_documentos.py   (cópia com HEAD)
7 failed in 0.16s

$ .venv/bin/python -m pytest -q
1289 passed, 2 skipped, 1 xfailed in 22.12s
```
(Baseline da revisão: 1282 passed. A diferença de +7 são os testes novos. As outras frentes editam em paralelo, então a contagem reflete a árvore como estava no momento.)
