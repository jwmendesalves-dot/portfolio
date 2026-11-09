# Frente J (onda 2) — job/ficha e identificação auxiliar

Status: **concluída** (escopo: só os meus arquivos). As pendências para outras frentes estão no fim.

## Resultado da suíte
- Linha de base, antes de editar: `.venv/bin/python -m pytest -q` → **14 failed, 728 passed**. Treze falhas eram desta frente: test_nome_regras 1, test_pasta_fichinha 1, test_servico_lote 3, test_telefones_fichinha 1 e test_variaveis_contrato 7. A 14ª é `test_ponte_fila::test_persistencia_atomica…`; ela lê código do agente-separador e é de outra frente.
- Meus arquivos de teste mais os vizinhos que usam `lote_para_job` → **283 passed, 1 skipped**. Os vizinhos são: variaveis_contrato, servico_lote, telefones_fichinha, pasta_fichinha, nome_regras, titulo_ficha, nome_do_cliente_prefixo, test_job_*, senha_cabecalho_curto, cabecalho_encaminhado, seguranca e limpeza_servico. As 13 falhas desta frente agora passam de verdade, com fixtures sintéticas e sem `/opt`.
- Suíte inteira (fechamento, 09/11 ~16:30): `.venv/bin/python -m pytest -q -p no:cacheprovider` → **949 passed, 2 skipped, 0 failed** (as 4 falhas de outras frentes vistas antes — test_ponte_rodada ×2 e test_triagem_download ×2 — já foram resolvidas pelas donas).
- Meus testes (variaveis_contrato, servico_lote, telefones_fichinha, pasta_fichinha, nome_regras, titulo_ficha, nome_do_cliente_prefixo, test_job_*) → **143 passed, 1 skipped** (o pulado exige `pdftotext`, ausente nesta máquina).
- `pyflakes` nos meus arquivos: limpo, exceto 3 variáveis locais sem uso em `lote_para_job.py` (`marcar`, `tel`, `alvo`) que **já existiam no HEAD** (conferido com `git show HEAD:lote_para_job.py | pyflakes`); não mexi para não alterar lógica fora do escopo. Os `import os` sem uso (pré-existentes) em test_nome_regras/test_titulo_ficha/test_nome_do_cliente_prefixo foram removidos.

## Por achado

| ID | Status | Arquivos | O que mudou | Testes |
|---|---|---|---|---|
| **ID-09 / OP-T3** | corrigido | nomear_lotes_por_cpf.py, lote_para_job.py | `nomear_lotes_por_cpf` não tem mais `WEBHOOK = _webhook()`, `SystemExit` nem `sys.path.insert("/opt…")`. As chamadas REST passam por `bitrix_cliente.listar`, importado só na hora do uso. Em `lote_para_job`, `codigo_pasta_do_cliente` não importa mais `nomear_lotes_por_cpf`: chama `bitrix_cliente` direto e captura `BaseException`, o que inclui o SystemExit legado (só KeyboardInterrupt sobe). Em caso de falha devolve `""`. | test_job_rede_f2: `test_importar_nomear_lotes_nao_resolve_webhook_nem_encerra`, `test_pasta_systemexit_legado_nao_derruba_montagem`, `test_pasta_sem_webhook_devolve_vazio` |
| **AUX-02** | corrigido | nomear_lotes_por_cpf.py | (1) O nome precisa ser **único**. O script junta os contatos das duas variantes de CPF, lendo todas as páginas via `listar`, e só aceita o nome se todos os nomes válidos forem equivalentes. Se divergirem, o resultado é `ambiguo` e não há queda para o deal. (2) Cache v2 no formato `{nome, origem, em}`. "Não achei" vale 72 h (ajustável por `NOMEAR_TTL_NEGATIVO_H`); nome achado vale 30 dias; o `""` do formato antigo é descartado. (3) Falha de rede, disjuntor aberto ou falta de webhook levantam `ConsultaFalhou`: a rodada para, nada vai ao cache como negativo e o processo sai com exit 1. (4) A gravação usa `estado_store.atualizar_lotes(..., dono="identificacao")` e escreve só `nome` e `nome_origem`. O backup passou a ser `estado_store.fazer_backup`, no lugar do `shutil.copy2`, e não há mais `json.dump` direto. O cache é gravado por `gravar_atomico`, com permissão 0600. | test_job_nomear_aux02 (10 testes) |
| **S01 (lado do job)** | parte segura corrigida e caminho mínimo implementado; **decisão pendente com o dono** | lote_para_job.py | Ver a seção S01 abaixo. | test_job_senha_s01 (13 testes + 1 pulado) |
| **S02 (meus arquivos)** | corrigido | nome_regras.py, lote_para_job.py, testes/* | Troquei 5 CPFs com DV válido por valores de `cpf_util.gerar_para_teste`. Tirei dos comentários de `lote_para_job.py` as senhas reais de clientes, o nome de cliente que aparecia na tabela de exemplo e nomes completos de clientes. Nos testes, os nomes que vinham junto com CPF viraram fictícios. No fechamento removi também um e-mail real de cliente num comentário de `lote_para_job.py` (virou `fulano@example.com`). Nenhum valor real foi copiado para este relatório. | Ver "Verificação de dados reais" abaixo |
| **F2-09** | corrigido | lote_para_job.py | O `main()` roda **offline**: liga `SEM_REDE` e restaura o valor anterior no `finally`. O docstring, a description e o epílogo do `--help` agora explicam que esta simulação não aplica os filtros da ponte e que a simulação fiel é `lote_para_fila.py --simular --todos --dias 7 --limite 4`. Corrigi também a menção a "worker parado". `montar_job` respeita `SEM_REDE` e a env `TRIAGEM_SEM_REDE`. | `test_main_simulacao_offline_e_marcada`, `test_help_aponta_a_simulacao_fiel`, `test_sem_rede_nao_rele_mensagem` |
| **F2-10** | corrigido na parte que nasce em montar_job | lote_para_job.py | O job real agora sai com o histórico `"enfileirado pela ponte do grupo"`. Só o `main()` de simulação marca "SIMULAÇÃO — não gravado". Cada item de `arquivos` ganhou o `file_id`. Com isso, `lote_para_fila.planejar` monta `staged_nome = <file_id>.<ext>` e o sidecar sai com `id` preenchido. | `test_job_real_sem_marca_de_simulacao_e_com_file_id`, `test_ponte_staging_usa_file_id` |
| **NR-01** | corrigido sem afrouxar a validação | nome_regras.py (usado também em nomear e montar_job) | Funções novas: `tokens_nome`, `chave_comparacao` e `nomes_equivalentes`. As partículas da/de/do/das/dos/e (sem ponto) são ignoradas. Uma inicial (`M.` ou `M`) casa com a palavra inteira, desde que: o número de tokens seja igual, o último sobrenome esteja por extenso e igual, haja pelo menos 2 nomes por extenso iguais e não haja inicial dos dois lados no mesmo token. A relação **não é transitiva**, de propósito. `validar_nome` e `parece_ruido` ficaram intocados. A comparação é usada em `nomear._nome_unico` (sempre ancorada no CPF) e para não gerar o aviso falso "nome da ficha não venceu". | test_nome_regras: 5 pares equivalentes, 8 casos de homônimo ou risco, `test_nr01_nao_transitivo_de_proposito` |
| **OP-T2** | corrigido | 5 arquivos de teste + `testes/fixtures/sinteticos.py` | Nenhum teste lê mais `/opt/triagem-bitrix/estado.json`. `lote_molde()` reproduz todas as chaves de `serializar_lote`, com nome fictício, CPF gerado, e-mail @example.com e senha fictícia. A tabela de variáveis é o texto `pdftotext -layout` equivalente: mesmos rótulos, tags `{UfCrm…}`, valor `0` tratado como lixo, celular repetido em "trabalho" e um terceiro número em "outro". O teste de telefones ficou mais forte: agora confere a ordem e os 2 números distintos. | — |
| **OP-T1** (caminhos) | corrigido nos meus arquivos | lote_para_job.py, nomear_lotes_por_cpf.py | Os caminhos agora vêm de `caminhos.BASE` e `caminhos.em_separador("data","staging")`; as envs `TRIAGEM_DIR` e `STAGING_DIR` continuam valendo. O cache fica em `caminhos.em_base(".cache_cpf_nome.json")` ou no caminho de `NOMEAR_CACHE`. | — |
| OP-T4 | não aplicável | — | É do test_monitor_lotes, que não é meu. | — |

### Evidência de que o código antigo tinha os defeitos
Rodei uma cópia do HEAD em scratch, com as fixtures novas e o `pdftotext` falsificado:
```
ANTIGO senha em variaveis_contrato: True
ANTIGO senha (valor) em avisos: True
ANTIGO historico: enfileirado (SIMULAÇÃO — não gravado)
ANTIGO file_id em arquivos: False
ANTIGO '[senha removida]' na fichinha: True
ANTIGO pasta com CPF 000.000.000-00 tenta consultar: True
```
Com o HEAD, `pytest testes/test_job_nomear_aux02.py` → `INTERNALERROR … SystemExit: webhook do Bitrix nao encontrado`. É a ID-09 reproduzida: o SystemExit derruba até a coleta do pytest.

**Reprova no original (fechamento, refeito):** cópia da árvore atual em scratch com `lote_para_job.py`, `nomear_lotes_por_cpf.py` e `nome_regras.py` trocados por `git show HEAD:<arquivo>`; testes novos rodados lá (scratch apagado depois).
- Original puro: test_job_rede_f2 → 11 errors; test_job_senha_s01 → 13 errors (o HEAD não tem a costura `_texto_pdf`); test_job_nomear_aux02 → `INTERNALERROR SystemExit: webhook do Bitrix nao encontrado`; test_nome_regras → ImportError (`chave_comparacao` não existe = NR-01 ausente).
- Original + shim **só de I/O** (pdftotext passa por `_texto_pdf`, atributo `SEM_REDE` inerte; `WEBHOOK` fixo em URL `.invalid` no nomear para conseguir importar):
  - **S01** `test_job_senha_s01` → **12 failed, 1 passed, 1 skipped**. Falham as asserções de senha em `variaveis_contrato`, senha no aviso, senha/CPF no `imprimir`/`--json`, `[senha removida]`/`***`/`[informada]` chegando à fichinha, releitura da mensagem. O que passa no original é `placeholder[S/SENHA]` (já era tratado).
  - **ID-09 / F2-09 / F2-10** `test_job_rede_f2` → **10 failed, 1 passed**: SystemExit legado derruba `codigo_pasta_do_cliente`/montagem, CPF inválido consulta, falha fica no cache, histórico com "SIMULAÇÃO", sem `file_id`, `main()` não offline, `--help` sem apontar a simulação fiel. O que passa é `test_ponte_staging_usa_file_id` (depende de `lote_para_fila`, já alterado por E).
  - **AUX-02** `test_job_nomear_aux02` → **10 failed**. Ressalva honesta: 3 deles falham por `AttributeError` (API nova `listar`/TTL que o HEAD não tem: `test_todas_as_paginas_contam`, `test_nao_achei_expira`, `test_simular_nao_grava_estado`); o motivo exato dos outros 7 (CPF duplicado ambíguo, partícula, queda para deal, falha ≠ "não achei", cache `""` legado, gravação via `estado_store`, rodada interrompida) não foi detalhado um a um; podem misturar asserção e API ausente. A prova forte de AUX-02 no original continua sendo o SystemExit na importação e a leitura do HEAD (primeiro contato vence; `""` cacheado para sempre; `json.dump` direto no estado).
- Os 4 testes antigos herméticos (variaveis_contrato, servico_lote, telefones_fichinha, pasta_fichinha) no original+shim → 11 failed, 3 passed.

## Verificação de dados reais (fechamento)
- Varredura nos 14 arquivos da frente por `\d{11}` e `000.000.000-00`, validando DV com `cpf_util.valido`: os únicos com DV válido (em `nome_regras.py`, test_pasta_fichinha, test_nome_regras, test_titulo_ficha, test_nome_do_cliente_prefixo) **não estavam no HEAD** e batem exatamente com `cpf_util.gerar_para_teste(9201…9205)`; `sinteticos.py` chama `gerar_para_teste(9101/9102)` em tempo de execução. Os demais (`000…`, `111…`, `123…901`) têm DV inválido e são casos negativos de propósito. Nenhum valor copiado para cá.
- Senhas/credenciais: nenhuma senha literal real, URL de webhook (`rest/<n>/<token>`) ou chave nos arquivos da frente. Restava um e-mail real de cliente em comentário (`lote_para_job.py`) → trocado por `fulano@example.com`. Nomes próprios restantes em comentários são só primeiros nomes de "casos" (ex.: "caso Samyra") — sem sobrenome, CPF ou contato.
- **PDFs em `testes/fixtures/` têm dado real** (sem pypdf/pdfplumber/pdftotext na venv/máquina; inspeção com `strings` + descompressão zlib dos streams, só contando indícios): `com_servico.pdf` — Producer Aspose.PDF, texto de ZapSign, **25 sequências de 11 dígitos com DV válido** e 13 e-mails no conteúdo; `sem_servico.pdf` — "Microsoft: Print To PDF", título de kit previdenciário real ("12- KIT PREV digital…"), 0 CPFs válidos detectáveis pelo método (o texto pode estar em glyphs sem ToUnicode simples). Nenhum teste referencia mais esses arquivos (grep `com_servico|sem_servico` só acha nomes de função). Ação: ver Pendência 1 (git rm + expurgo de histórico pelo orquestrador).

## S01 — investigação e decisão
**Contrato do painel (verificado):** li uma cópia do agente-separador de 25/09 que estava em `~/Downloads/automacao-triagem-fonte.tar.gz`. Extraí em scratch, só para leitura, e apaguei depois. O fluxo é este:
- `extract-data.js:270-277` lê `SENHA…` do texto da fichinha;
- `app2-client.js:471` grava a senha no contato do Bitrix (`UF_CRM_1725391896584`);
- `app2-client.js:961-996` grava na planilha, coluna H, e trata `S/SENHA` como "sem senha", **preservando** a senha que já existe lá;
- o painel não lê `variaveis_contrato` (o grep não achou nada nessa cópia).

Conclusão: o painel **exige** a senha, e só na `fichinha`, na linha `NIS/Senha do INSS`.

**Implementado:**
1. A senha só entra na `fichinha`. Saiu de `variaveis_contrato`. O aviso de divergência diz "valores omitidos". `imprimir()` e `--json` passam por `para_exibir()`, que faz uma cópia com `seguranca.mascarar`: senha vira `***` e o CPF é mascarado. O job real não é alterado.
2. Quando a ficha guardada traz `[senha removida]` (efeito de C1), `montar_job` chama `senha_da_mensagem_original(lote)`. Isso só acontece com rede, nunca na simulação. A função faz uma leitura `bitrix_cliente.chamar("im.dialog.messages.get", {"DIALOG_ID": lote.dialog_id, "FIRST_ID": id-1, "LIMIT": 1})`, passa o texto por `parse_ficha` e põe a senha só na `fichinha`, sem cache, sem log e sem gravar em lugar nenhum.
3. Se não recupera a senha, a linha sai sem senha: vazia (e então `fichinha_sem_vazios` a remove) ou no formato `"<nis> / S/SENHA"`. Vai junto um aviso para a operadora ver a mensagem original. Marcadores como `[senha removida]`, `***` e `[informada]` **nunca** chegam ao painel, porque ele os gravaria como senha no contato.
4. A tabela do contrato assinado continua vencendo a ficha. Ela é lida do PDF no disco na hora de montar o job, então esse caminho não depende do que fica guardado.

**DECISÃO PENDENTE DO DONO — senha gov.br no job:**
- O webhook enxerga só **parte** do grupo. Pela medição de 21/09, perdia 62 de 85 mensagens, justamente os kits. Então a releitura pode falhar com frequência. Opções:
  - (a) aceitar como está: S/SENHA + aviso — zero segredo novo em repouso; custo: operadora completa à mão quando a releitura falha (o painel **preserva** a senha já existente na planilha quando recebe S/SENHA, então não apaga dado bom);
  - (b) reler pelo VibeCode `GET /v1/chats/{dialogId}/messages?firstId=`, o que exige a `vibecode_key` na ponte — cobre o grupo inteiro, mas espalha mais uma credencial;
  - (c) C1 guardar a senha **cifrada**, com a chave fora do disco de dados — automação total, porém volta a existir senha em repouso e gestão de chave;
  - (d) parar de levar a senha ao painel e a operadora preenche à mão — mais seguro, mais trabalho manual sempre.
- **Recomendação de J:** **(a) agora** (é o que está implementado e não guarda senha em lugar nenhum novo), e **(b) como evolução** se a taxa de S/SENHA medida no servidor for alta, já que a ponte lê do mesmo VibeCode que a triagem usa; evitar (c) enquanto não houver custódia de chave. Quem decide é o dono; até lá o comportamento é (a).
- `fila-jobs.json` e os `backups/fila-jobs-*` continuam com a fichinha **com** senha, porque o painel precisa dela ali. Retenção e expurgo desses arquivos ficam com E/S05.

## Arquivos alterados/criados (frente J)
- Alterados: `lote_para_job.py`, `nomear_lotes_por_cpf.py` (reescrito), `nome_regras.py`, `testes/test_variaveis_contrato.py`, `testes/test_servico_lote.py`, `testes/test_telefones_fichinha.py`, `testes/test_pasta_fichinha.py`, `testes/test_nome_regras.py`, `testes/test_titulo_ficha.py`, `testes/test_nome_do_cliente_prefixo.py`.
- Criados: `testes/fixtures/sinteticos.py`, `testes/test_job_senha_s01.py`, `testes/test_job_rede_f2.py`, `testes/test_job_nomear_aux02.py`, este relatório.
- A assinatura `montar_job(lote, quando=None)` e as chaves do job não mudaram: `variaveis_contrato` continua existindo, só sem a sub-chave `senha`. Os itens de `arquivos[]` do envelope ganharam `file_id`.

## Mudanças de comportamento (deploy)
- `lote_para_job.py` (CLI) roda offline: a pasta sai sempre vazia, e CPF e senha saem mascarados, também no `--json`.
- O aviso de divergência de senha não traz os valores, e `variaveis_contrato` não tem mais `senha`.
- Job da ponte: o histórico agora é "enfileirado pela ponte do grupo", o sidecar tem `id` e os arquivos no staging levam o nome do `file_id`.
- `codigo_pasta_do_cliente`: CPF com DV inválido não gera consulta; falha não fica no cache; respeita `TRIAGEM_SEM_REDE`.
- `nomear_lotes_por_cpf`:
  - CPF duplicado com nomes diferentes não grava mais;
  - não mexe em `nomes_candidatos`, porque esse campo é da triagem;
  - o backup vai para `backups-estado/` com retenção;
  - aceita `--estado`;
  - sai com exit 1 se a rodada for interrompida;
  - entradas `""` do cache antigo são consultadas de novo uma vez.

## Pendências para outras frentes / orquestrador
1. **Orquestrador (S02):** `testes/fixtures/com_servico.pdf` e `sem_servico.pdf` são **PDFs reais de contrato de cliente**: gerados por Aspose/ZapSign, com nome, CPF de DV válido, e-mail e senha gov.br. Nenhum teste os usa mais. É preciso `git rm` e expurgar do histórico (estão no commit baseline `fbc440c`). O texto antigo de `test_variaveis_contrato.py`, que tinha a senha e o CPF da cliente, também está no baseline. A senha deve ser tratada como comprometida e a cliente, avisada. Não apaguei nada, porque a regra proíbe git de escrita.
2. **Dono de `testes/test_senha_cabecalho_curto.py` (arquivo que não é meu):** usa a senha real de um cliente ("caso Júlio") e um CPF; precisa trocar por valores fictícios.
3. **E (`lote_para_fila.py`):**
   - (a) `imprimir_plano(..., mostrar_ficha=True)` imprime a fichinha **com a senha**; passar antes por `L.para_exibir(sim)` ou `seguranca.mascarar`;
   - (b) se `--simular` deve ser offline, definir `L.SEM_REDE = True`;
   - (c) o histórico já vem neutro de `montar_job`;
   - (d) o sidecar já recebe `file_id`;
   - (e) os backups `fila-jobs-*` e `/tmp/_entradas.json` contêm a senha (S01/S05).
4. **F (`completar_cpf_por_nome.py`):** se for usar `nome_regras.nomes_equivalentes`, exigir candidato **único** (a relação não é transitiva) e manter a 2ª âncora de ID-02.
5. **C1:** o lado do job já aceita `[senha removida]` (= `seguranca.SENHA_REMOVIDA`). A releitura depende de `lote.dialog_id` e `mensagens[].id`: é preciso mantê-los em `serializar_lote`.
6. **`nome-regras.js`:** `nomes_equivalentes` não faz parte do contrato de normalização v1, então não precisa de espelho no JS.

## Riscos / só o servidor confirma
- Não há `pdftotext` nesta máquina. O teste `test_pdftotext_real_le_a_tabela_do_contrato_sintetico`, que usa um PDF sintético gerado com stdlib, é pulado aqui e roda no servidor. Os demais testes injetam o texto `-layout` em `_texto_pdf`.
- Não confirmei se `im.dialog.messages.get` com `FIRST_ID=id-1` devolve a mensagem da ficha (ver a limitação do webhook acima).
- A cópia do painel que conferi é de 25/09, anterior à decisão de 04/11 (NIS e senha na mesma linha). O formato `<nis> / S/SENHA` segue o código de `_nis_e_senha` que já existia.
