# Frente C4 (onda 4): correções do cenário integral (G-01, G-02, G-03, cpfs_invalidos, teste de tempo)

Status: **CONCLUÍDO**. A suíte inteira passa duas vezes seguidas, e os dois `xfail(strict)` da frente G viraram testes normais e passam.

## Arquivos alterados/criados
- `triagem_grupo.py`: `_parse_data`, `_lote_recente_dl` (no `ciclo`) e o tratamento de erro de download (`baixar_file_id_api`, onde se decide `erro_tipo`).
- `download_svc.py`: só a marcação de mídia em `processar_item`, ou seja, o patch que vai para `_aplicar_no_estado`.
- `ponte_criterio.py`: `tipo_arquivo`.
- `estado_store.py`: `PROPRIEDADE_CAMPOS` (`cpfs_invalidos`).
- `testes/test_cenario_integral.py`: tirei os xfail e desfiz os contornos que existiam por causa de G-01/G-02.
- `testes/test_rede_bitrix.py`: só o teste instável do limitador.
- `testes/test_g_correcoes.py` (**novo**, 9 testes).

## Por achado

### G-01 · corrigido · `triagem_grupo.py`
- **Causa:** `lote["fim"]` chega como `datetime` e `_parse_data` só aceitava texto. Com isso devolvia `None`, `_lote_recente_dl` dava sempre `False` e o ciclo nunca baixava nada.
- **O que mudou:**
  - `_parse_data` devolve o próprio `datetime` quando recebe um.
  - `_lote_recente_dl` trata `datetime` ingênuo como UTC, a mesma convenção de `dia_local` e `_fim_do_lote`. Antes, um ingênuo comparado com o `_agora_dl` aware levantaria `TypeError`.
- **Mudança de comportamento em produção:** o ciclo volta a baixar no próprio ciclo os kits recentes (≤ 3 dias), respeitando o teto `TRIAGEM_MAX_DOWNLOADS_CICLO` (120). Esse caminho estava morto desde 27/10. O `baixar_pendentes` continua cobrindo os buracos.
- **Testes:** `test_g01_*` em `test_g_correcoes.py`, `test_cenario_integral.py::test_g01_ciclo_baixa_kit_recente` (sem xfail) e o cenário principal, que agora confere sha256 dos documentos **logo depois do ciclo 1, antes do `baixar_pendentes`**.

### G-02 · corrigido (duas camadas) · `download_svc.py` + `ponte_criterio.py`
- **Camada (b), `download_svc`:** quando marca mídia, o download grava também `tipo='midia'` (`tipos_documento.MIDIA`) e `nome_original` (basename do `NAME`), além de `erro_tipo='midia'`.
  - `tipo` e `nome_original` são campos da triagem. O merge por campo só os preenche se estiverem vazios, então um `tipo` que a triagem já gravou é preservado. Isso tem teste (`test_g02_download_nao_sobrescreve_tipo_da_triagem`).
- **Camada (a), `ponte_criterio.tipo_arquivo`:** `erro_tipo=='midia'` passa a valer como mídia. A regra vale para item sem `tipo` e também sobre um `tipo='desconhecido'` provisório. Um `tipo` explícito `documento`/`midia` continua mandando.
  - Essa camada é defensiva para os lotes já gravados em produção, que só têm `erro_tipo`.
- **Testes:**
  - `test_g02_*` em `test_g_correcoes.py`, cobrindo ponte, `lote_documental`, `processar_item` + `_aplicar_no_estado` e a não sobrescrita.
  - `test_cenario_integral.py::test_g02_kit_com_audio_vai_a_fila` sem xfail. A asserção `erro_tipo == "midia"` virou `"midia" in (tipo, erro_tipo)`, porque com o G-01 corrigido quem marca o áudio passa a ser o ciclo (`tipo='midia'` via `_meta_midia`).
- **Contorno desfeito no cenário:** o áudio (`103`) voltou para o **kit principal K0**; o K6 ficou só com o documento complementar. O cenário prova que:
  - o áudio é mídia, sem GET e sem sha256;
  - **o K0 com áudio vai à fila** na ponte do meio;
  - o staging do K0 tem só os 3 documentos;
  - o K5 (reenvio idêntico) continua `descartado_duplicata`.

### G-03 · corrigido · `triagem_grupo.py`
- **O que mudou:** em `baixar_file_id_api`, `erro_tipo='permanente'` agora vale quando `erro_bitrix.permanente`. Esse campo é o `rb.classificar(...).permanente`, a mesma régua do `download_svc`. O teste antigo de `_CLASSES_PERMANENTES_ITEM` foi mantido em OR. Com isso, um permanente não chama `_enfileirar_retry`, e no ciclo seguinte o erro definitivo é reaproveitado sem nova chamada.
- **Testes:**
  - `test_g03_access_denied_no_ciclo_e_permanente_e_nao_vai_ao_retry`: permanente, retry vazio, e o 2º ciclo não repete.
  - `test_g03_transitorio_continua_indo_ao_retry` (guarda): 503 QUERY_LIMIT continua transitório e vai ao retry.
  - No cenário principal, K4/`502` agora tem `erro_tipo == "permanente"` e `"502" not in retry`.
- **Risco (registrar):** `rb.PERMANENTES` inclui `OVERLOAD` (portal bloqueado) e `ACESSO` de sistema (401, token revogado). Nesses casos o item do ciclo também vira permanente, exatamente como já acontece no `download_svc`. Se a credencial cair, os kits recentes desse ciclo ficam marcados como permanentes e precisam de reprocessamento manual (`--force`). Se preferirem, o refinamento é excluir `pausa_rodada=True` (erro de sistema, não de item). Não fiz isso porque o briefing pedia alinhamento com o `download_svc`.

### cpfs_invalidos · corrigido · `estado_store.py`
- Declarado como `"cpfs_invalidos": "triagem"`.
- **Justificativa:** quem grava é a triagem (C2b, `classificar_mensagem` e a montagem do lote). O campo é recalculado do texto a cada ciclo, na mesma família de `cpfs`/`cpfs_papeis`. Se o dono fosse a identificação, uma reivindicação congelaria o valor e o ciclo deixaria de refletir mensagem nova ou editada. Nenhum outro módulo grava esse campo; o `manutencao/inventario_estado.py` usa uma variável local homônima, sem relação.
- **Varredura de avisos "sem dono"** com `pytest -q -o log_cli=true --log-cli-level=INFO`: antes apareciam `cpfs_invalidos` (2×) e `campo_novo_x` (1×). Agora só sobra `campo_novo_x`, que é intencional: `test_estado_store.py:376` testa justamente o aviso. Os campos novos que o download grava (`tipo`, `nome_original`) já estavam declarados.
- **Teste:** `test_cpfs_invalidos_tem_dono_declarado`.

### Teste instável · estabilizado · `testes/test_rede_bitrix.py::test_limitador_compartilhado_entre_processos_respeita_taxa_e_rajada`
- **Causa:** a marca de tempo é tirada **depois** de `aguardar()` voltar. Sob carga, o processo pode perder a CPU entre a concessão e a marca, e as marcas se aproximam. Com taxa 25/s e folga 0,02 s (0,5 token) isso aparecia como excesso.
- **O que mudou:**
  - Taxa 10/s, rajada 5, 3×8 pedidos (~2,1 s).
  - Folga de 0,15 s (1,5 token) aplicada igual na checagem de janela e na de duração mínima.
  - A invariante continua a mesma: `liberados ≤ rajada + taxa·(Δt + folga)` em toda janela.
- **Continua provando o que deve:** um mutante com balde **não compartilhado** (arquivo por processo) **reprova**.
- **Evidência:**
  ```
  5× seguidas:  1 passed in 2.19s / 2.10s / 2.10s / 2.09s / 2.09s
  sob carga (6 execuções paralelas + 6 `yes`): ['1 passed in 2.49s', '1 passed in 2.43s', '1 passed in 2.44s', '1 passed in 2.43s', '1 passed in 2.47s', '1 passed in 2.52s']
  mutante balde por processo: ['1 failed in 0.60s']
  ```

## Prova de que os testes falham no HEAD
Fiz uma cópia em tmp do repositório com `git show HEAD:<f>` para `triagem_grupo.py`, `download_svc.py`, `ponte_criterio.py` e `estado_store.py`, e rodei os testes novos e o cenário:
```
FAILED testes/test_g_correcoes.py::test_g01_parse_data_aceita_datetime_aware
FAILED testes/test_g_correcoes.py::test_g01_parse_data_aceita_datetime_ingenuo_e_texto
FAILED testes/test_g_correcoes.py::test_g02_ponte_trata_erro_tipo_midia_como_midia
FAILED testes/test_g_correcoes.py::test_g02_lote_com_audio_marcado_pelo_download_nao_fica_pendente
FAILED testes/test_g_correcoes.py::test_g02_download_marca_midia_com_tipo_e_nome_no_estado
FAILED testes/test_g_correcoes.py::test_g03_access_denied_no_ciclo_e_permanente_e_nao_vai_ao_retry
FAILED testes/test_g_correcoes.py::test_cpfs_invalidos_tem_dono_declarado - A...
FAILED testes/test_cenario_integral.py::test_cenario_integral_tres_ciclos_reinicio_falha_de_gravacao_e_ponte
FAILED testes/test_cenario_integral.py::test_g01_ciclo_baixa_kit_recente - As...
FAILED testes/test_cenario_integral.py::test_g02_kit_com_audio_vai_a_fila - A...
10 failed, 2 passed in 0.35s
```
Os 2 que passam no HEAD são guardas de não regressão: `nao_sobrescreve_tipo_da_triagem` e `transitorio_continua_indo_ao_retry`.

## Evidência final
```
$ .venv/bin/python -m pyflakes triagem_grupo.py download_svc.py ponte_criterio.py estado_store.py testes/test_cenario_integral.py testes/test_rede_bitrix.py testes/test_g_correcoes.py
pyflakes rc=0
$ .venv/bin/python -m pytest -q        # 1ª
1104 passed, 2 skipped, 1 xfailed in 16.56s
$ .venv/bin/python -m pytest -q        # 2ª
1104 passed, 2 skipped, 1 xfailed in 16.08s
```
Antes eram 1093 passed e 3 xfailed. A conta fecha: +9 testes novos e +2 ex-xfail (G-01, G-02). O xfail restante não é desta frente.

## Pendências e riscos
- **Risco G-03:** ver acima. `OVERLOAD`/401 também marcam o item do ciclo como permanente, igual ao `download_svc`. Decisão de produto: excluir `pausa_rodada`?
- **Volta do download no ciclo (G-01):** o ciclo volta a fazer HTTP de download (até 120 por ciclo, com teto de tempo por item, a mesma lógica de 27/10). Vale observar a duração do ciclo no servidor nas primeiras horas.
- **Lotes já gravados com áudio parado em `AGUARDANDO_DOWNLOAD`:** a camada (a) da ponte os libera na próxima rodada, sem migração.
- **Precisa do servidor:** confirmar com o log real que o ciclo passa a registrar `[OK]`/`webhook:disk.file.get` nos kits recentes, e que não sobra aviso "sem dono" no journal.

## Decisão do orquestrador (pós-C4)
O ponto "Para decidir" foi resolvido assim: no ciclo, erro com `pausa_rodada=True` (401 token revogado, OVERLOAD, limite) **não** marca o arquivo como permanente — fica transitório. Motivo: é falha de sistema, não do arquivo; marcar permanente faria uma credencial caída travar todos os kits recentes e exigiria `--force` manual. Teste: `testes/test_g_correcoes.py::test_g03_erro_de_sistema_nao_marca_arquivo_permanente` (401 e OVERLOAD; falha sem a guarda). ACCESS_DENIED de arquivo continua permanente.
