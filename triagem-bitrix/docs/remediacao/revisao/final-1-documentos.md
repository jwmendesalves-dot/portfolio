# Revisão final independente — foco 1: perda/troca de documento

- Branch `remediacao`, HEAD `ce6cc54`, diff revisado `fbc440c..HEAD`.
- Escopo: `triagem_grupo.ciclo` (cursor FIRST_ID, `f1_regiao`, id de lote, lacuna), `download_svc` (dedupe, permanente × transitório, merge no estado), `ponte_criterio`, `lote_para_fila --enviar`, `completar_cpf_por_nome`, `estado_store`.
- Modo: somente leitura. Nenhum código do repo foi editado e não houve git de escrita. Testes temporários em `~/.hermes/cache/scratch/revfinal/test_rev_documentos.py` (fora do repo; 9 testes, todos passam: 3 reproduzem defeito e 6 confirmam garantias).

## Suíte

```
$ .venv/bin/python -m pytest -q
...
1282 passed, 2 skipped, 1 xfailed in 17.73s
```
(o baseline no início deu o mesmo resultado: `1282 passed, 2 skipped, 1 xfailed in 18.03s`, EXIT=0)

```
$ cd ~/.hermes/cache/scratch/revfinal && .venv/bin/python -m pytest -q -s test_rev_documentos.py
com cache: confirmar | verdade: sugerir
...situacao_kit: repetido | pode_enfileirar: (False, ['lote não está fechado', ..., '1 file_id(s) sem registro de arquivo'])
.conflitos: {'LOTE-VELHO': ['<lote inexistente>']} | meta: {'file_id': '77', 'indice': 1}
.....
9 passed in 0.05s
```

## Achados

### A1 · MÉDIA · cache de telefone contaminado faz uma sugestão virar CPF confirmado
- **Onde:** `completar_cpf_por_nome.py:259-273` (`contatos_por_telefone`).
- **O defeito:** `crm.duplicate.findbycomm` consulta todos os telefones que faltam numa única chamada, e a resposta não diz qual valor casou com qual contato. Mesmo assim, o código grava o conjunto **inteiro** como resultado de **cada** telefone (`for t in faltam: cache.guardar("fones", t, "contatos", achados)`). O comentário da linha 271 reconhece isso. Daí em diante, por até `TTL_POSITIVO_S`, um telefone que é do contato B passa a "pertencer" também ao contato A.
- **Efeito:** um lote "Joao Silva" que traz só o telefone de outra pessoa (B) recebe `acao=confirmar`. Com isso grava `cpf`, `identificacao=OK` e `cpf_origem` confirmada, e o lote fica elegível para a fila. Sem o cache o resultado seria `sugerir` (só `cpf_sugerido`, sem envio). Ou seja, o telefone, que é a prova que separa homônimos, passa a valer como prova sem ser.
- **Reprodução:** `test_cache_de_telefone_contaminado_promove_sugestao_a_confirmacao`. O lote 1 consulta os telefones X (do B) e Y (do A); o lote 2 é "Joao Silva" só com X. Saída: `com cache: confirmar | verdade: sugerir`.
- **Correção sugerida:** consultar `findbycomm` **um telefone por chamada** (2 valores: `t` e `+55t`) e guardar o resultado só para aquele telefone. Alternativa: só gravar no cache quando `len(faltam) == 1`; com mais telefones, usar o conjunto apenas nesta decisão, sem cachear. Também convém invalidar o cache `fones` atual no deploy (`cache-cpf-nome.json` → apagar a chave `fones`). Teste de regressão: o mesmo cenário acima, que deve terminar em `sugerir`.

### A2 · BAIXA · `situacao_kit`/`kit_completo` não conferem `file_ids` (contrato da docstring)
- **Onde:** `ponte_criterio.py:160-183`.
- **O defeito:** a docstring de `situacao_kit` diz que "Só existe 'repetido' com o kit 100% baixado". Porém `kit_completo` só examina `arquivos`. Um lote com um `file_id` sem meta (documento que ainda nem foi registrado) devolve `repetido`.
- **Por que é só baixa:** no `selecionar` (`lote_para_fila.py:868`/`901`), `avaliar` → `estado_lote.pode_enfileirar` → `_problemas_arquivos` exige meta para todo `file_id` (`estado_lote.py:121-126`) **antes** de `situacao_kit` decidir o descarte. Hoje o descarte de duplicata só acontece com o kit 100% baixado. O risco é um consumidor futuro chamar `situacao_kit` direto (o `monitor_lotes` já importa o módulo).
- **Reprodução:** `test_situacao_kit_diz_repetido_com_file_id_sem_meta`, com saída `situacao_kit: repetido | pode_enfileirar: (False, [... '1 file_id(s) sem registro de arquivo'])`.
- **Correção sugerida:** em `kit_completo`, devolver `False` quando algum `file_id` de `lote_documental(lote)["file_ids"]` não tem meta em `arquivos`.

### A3 · BAIXA · desfecho do download com `lote_id` que não bate é descartado e o item fica `baixado` na fila
- **Onde:** `download_svc.py:950-978` (`_aplicar_no_estado`), junto com `estado_store.py:690-694`.
- **O defeito:** quando o item da fila tem `lote_id`, o mapa `file_id → lote` do estado não é consultado. Se aquele lote não existe, `atualizar_lotes` responde `<lote inexistente>` e o meta (sha256/caminho) não chega ao lote que de fato contém o `file_id`. A fila já marcou o item como `BAIXADO`, então ele não volta a ser tentado, e o kit fica em AGUARDANDO_DOWNLOAD.
- **Quando acontece:** pouco. O ciclo só baixa (e enfileira retry, `triagem_grupo.py:2937`, com `lote_id=lote["lote_id"]` via `:3483`) os lotes de `_filtrar_lotes_incrementais`, que são gravados no mesmo ciclo, e o `_lote_id` (autor+id da 1ª mensagem, `:1691`) é estável. Sobram dois casos: a gravação do estado falhar depois do enfileiramento, ou o lote ser dividido (`dividir_lote_misto`: o patch cai no pai e o sub-lote não recebe nada). O arquivo **não se perde**: fica no disco, e o `reconciliar_por_file_ids.py --dias 7 --aplicar` do `pipeline.sh:34` recupera por prova (`NN_`/nome/sha) dentro de 7 dias.
- **Reprodução:** `test_desfecho_do_download_com_lote_id_velho_se_perde`, com saída `conflitos: {'LOTE-VELHO': ['<lote inexistente>']} | meta: {'file_id': '77', 'indice': 1}` (o meta continua sem `baixado`).
- **Correção sugerida:** em `_aplicar_no_estado`, quando o conflito for `<lote inexistente>` (ou "file_id fora do lote"), reaplicar pelo mapa `_indice_estado` (lote que contém o `file_id`; se o lote achado estiver DIVIDIDO, usar o sub-lote em `dividido_em` que tem o fid). Se ainda assim não aplicar, logar com nível de erro e deixar o item numa marca `estado_pendente=True` para a próxima rodada reaplicar.

## Verificações que passaram (conferidas no código e, onde indicado, por teste)

- **Cursor/lacuna (`triagem_grupo.py:1540-1646`, `:3272-3331`, `:3511-3530`):** com FIRST_ID, a leitura sobe a partir do maior id recebido e a parada é "nenhum id novo", nunca `len == LIMIT`. FIRST_ID apagado é tratado pela sonda, que cai na leitura descendente até o cursor. Se as páginas acabam antes do cursor → `lacuna`: o cursor e a região não avançam e um alerta sai. Erro de leitura não grava nada.
- **Região/ids (`:3155-3214`):** um lote fechado no estado não é reescrito. Lote novo que reaproveita mensagens de um fechado ou é descartado (se o que sobra não tem material) ou nasce em revisão. O id do lote (`:1691`) depende só da 1ª mensagem, que a região preserva.
- **E3, mesmo nome com outro file_id:** `_enfileirar_em` (`download_svc.py:715-719`) e `_resolver_destino` (`:447-458`) nunca sobrescrevem nem reaproveitam o arquivo de outro item (`test_mesmo_nome_outro_file_id_nao_sobrescreve`).
- **Permanente × transitório (`download_svc.py:902-929`):** permanente só quando nenhum erro é transitório e nenhuma fonte foi pulada; permanente nunca volta a pendente (`:735-736`); fontes indisponíveis não contam tentativa.
- **Merge no estado:** o download é monotônico. A triagem não rebaixa um meta `baixado`+sha256 (`estado_store.py:515-519`; `test_download_nao_rebaixa_meta_baixado_pela_triagem`). Estado corrompido levanta exceção e nada é gravado (`test_estado_corrompido_nao_vira_vazio`). A triagem não sobrescreve CPF reivindicado pela identificação (`test_triagem_nao_sobrescreve_cpf_confirmado`).
- **Homônimo:** CPFs diferentes, ou um contato sem CPF, dão `ambiguo`. Sem `cpf`, com `cpf_sugerido=None`, o lote não é enviável (`test_homonimos_cpfs_diferentes_nunca_recebem_cpf`, `test_homonimo_sem_cpf_bloqueia`). Resultado truncado também dá `ambiguo`. `sugerir` nunca grava `cpf`. A exceção é o A1.
- **Ponte:**
  - `avaliar` exige identidade OK, CPF válido e todo documento com meta, baixado, sha256 e tamanho > 0, mesmo com `--forcar`.
  - O descarte de duplicata só acontece com o kit completo, e o que estiver incompleto vai para AGUARDANDO_DOWNLOAD.
  - O descarte grava status com dono `ponte`, e lote descartado que depois aparece com documento novo vai para `reavaliar`.
- **`--enviar`:**
  - Exclusão por `fila.lock` (trava herdada do shell conferida por dev/inode).
  - `registrar_enfileirando` acontece antes do staging. O staging é limpo antes de montar, conferido por contagem e desfeito na falha.
  - `upload_id` é uuid5 de lote + conjunto de sha256, então é idempotente.
  - A confirmação lê o que está na fila, e o registro órfão é desfeito com o staging removido.
  - `planejar` recusa sha256 do disco diferente do registrado e arquivo ausente ou de 0 byte. Os metas ficam alinhados 1:1 com `montar_job` (`lote_para_job.py:921`).

## O que NÃO testei

- Servidor real, Bitrix/VibeCode reais e o `FilaJobs.enfileirar` do painel em Node (merge por id e gravação atômica do lado do painel).
- Ciclo completo com `dividir_lote_misto` + download_svc concorrente (A3 foi provado só na função).
- Corrida real entre ciclo e `download_svc` gravando o mesmo `file_id` (possível arquivo duplicado `__fid` no disco; não é perda).
- `docs_posteriores`: documento que chega depois do envio só é listado, não enviado. É decisão registrada, não defeito, mas depende de alguém ler `fila-enviados.json`.

## Veredito (foco 1)

**APROVADO COM RESSALVAS.** Não achei caminho de perda silenciosa de documento nem de troca de arquivo entre clientes ou anexos: os portões de sha256, file_id, kit completo e dono de campo se sustentam. Corrigir antes do deploy:

1. **A1** (`completar_cpf_por_nome.py:259-273`): cache de telefone por consulta individual + limpar o cache `fones` atual. É o único caminho achado em que um CPF é confirmado com prova falsa.
2. **A3** (`download_svc.py:950-978`): reaplicar pelo mapa do estado quando o lote do item não existe ou não contém o fid (hoje depende do reconciliador de 7 dias).
3. **A2** (`ponte_criterio.py:160-173`): `kit_completo` também conferir `file_ids` (endurecimento do contrato).
