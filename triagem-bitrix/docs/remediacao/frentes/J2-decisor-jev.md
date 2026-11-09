# Frente J2 — decisor TypeSafe/JEV (cliente + decisões + modo sombra)

Status: **implementado, desligado por padrão, testado só offline (transporte falso). Nenhuma chamada real.**
Sem commit. Sem alteração em `config.json`, `lote_para_fila.py` nem arquivos da Z3.

## Arquivos
| arquivo | tipo | o quê |
|---|---|---|
| `typesafe_cliente.py` | novo | cliente stdlib (urllib) `perguntar`, `modelos`, `validar_questoes`, `carregar_config`, `ler_chave`; erros classificados |
| `decisoes_jev.py` | novo | 3 decisões (montar/rotear/decidir), limiares em constantes, modos desligado/sombra/decide, JSONL |
| `seguranca.py` | acréscimo | máscara `apikey_<...>` em texto livre; `typesafe_key` em `NOMES_CONHECIDOS` e legados padrão (`caminhos.em_base("typesafe.key")`, `/root/.typesafe-jev.key`) |
| `testes/test_typesafe_cliente.py` | novo | shape, erros HTTP, retry/Retry-After, chave fora de log/exceção |
| `testes/test_decisoes_jev.py` | novo | roteamento, regra de ouro, sombra, falha, PII |
| `testes/test_seguranca.py` | acréscimo | casos `apikey_` e `Authorization: Bearer` |

## Contrato conferido na doc viva (docs.typesafe.ai, 09/11/2026)
Confere com o briefing. Diferenças/acréscimos registrados:
- `noul` aceita `criteria` **opcional** `{true, false}` — validado.
- Existe um 4º tipo `box` (localização em imagem) — fora do escopo; a validação local recusa.
- `score` é **0-indexado** e pode ser fracionário (`legend` "0","1",...). Limiar de risco usa isso.
- 422 de validação traz `detail[].input` com **trecho da entrada** (pode ecoar o state/CPF): o cliente só aproveita `type@loc`.
- A doc recomenda, para "nenhuma opção serve", um **Noul companheiro** em vez de opção `indefinido`/`other` (compete por probabilidade e derruba a confiança). Mantive `indefinido`/`sem_elementos` conforme o briefing; com isso a confiança cai mais vezes ⇒ mais revisão humana (lado seguro). Candidato a ajuste depois da medição em sombra.
- Confiança de choice = `(p_max − 1/n)/(1 − 1/n)`: com 3 opções, 0,85 exige p_max ≈ 0,90.

## Configuração (bloco novo em `config.json` — NÃO aplicado; dono do config é o orquestrador)
```json
"typesafe": {"ativo": false, "modo": "desligado", "base_url": "https://api.typesafe.ai",
             "model": "jev-latest", "key_file": null, "timeout": 20,
             "sombra_jsonl": null}
```
Bloco ausente/ilegível ⇒ padrões acima (ativo=false). `ativo != true` força modo `desligado` e `perguntar` levanta `TypeSafeDesligado` sem tocar rede.
`sombra_jsonl` null ⇒ `caminhos.em_base("jev-decisoes.jsonl")` (0600, append).

## Chave
`seguranca.ler_segredo("typesafe_key")`: env `TRIAGEM_TYPESAFE_KEY` → `/etc/triagem-bitrix/typesafe_key` (recusa 0o077) → legados `/opt/triagem-bitrix/typesafe.key`, `/root/.typesafe-jev.key` (aviso; `TRIAGEM_SEGREDOS_ESTRITO=1` ⇒ `SegredoRecusado`) → `typesafe.key_file` (mesmas checagens via `seguranca._ler_arquivo`). Lida só depois da validação de shape e do teste `ativo`.

## Erros (cliente)
- 401/403 → `ErroCredencial` (permanente, sem retry).
- 429/529/500/502/503/504/timeout/rede → `ErroTransitorio`; até **2** repetições, pausa 0,5 s/1 s ou `Retry-After`, teto 5 s.
- outros 4xx (400/404/422 `dict_type`, `union_tag_not_found`) → `ErroRequisicao` (erro de programação, sem retry).
- 2xx sem JSON / sem todas as answers → `ErroResposta`.
- Mensagens passam por `seguranca.mascarar`; nunca contêm chave nem state. Log de sucesso: ids das questões, modelo, latência, tokens.

## Decisões e limiares (`decisoes_jev.py`)
`CONFIANCA_MINIMA_PARA_AGIR = 0.85`, `RISCO_MAXIMO_ACEITAVEL = 1.5` (score 0..3), `LIMITE_TRECHO_OCR = 1500`.

**(a) `documento_e_do_cliente(cliente, trecho_ocr)`** — choice `dono_documento` {mesmo_cliente, outro_cliente, indefinido} + score `risco_documento_terceiro` (4 níveis).
Regra determinística (fato via `cpf_util.valido` sobre CPFs do OCR):
- `sim` → `manter_no_kit`; `nao` → `quarentena`; `ambiguo` (o do cliente + outro) → `revisao_humana`; `ausente` → `quarentena`.
- Com fato `sim`/`nao`/`ambiguo` a **regra decide sempre** (fonte `regra`); o JEV só confirma (`divergencia_jev` registrado).
- Fato `ausente`: confiança < 0,85 → `revisao_humana`; `outro_cliente` → `quarentena`; `mesmo_cliente` → **`revisao_humana`** (nunca `manter_no_kit`); risco alto/indefinido → `revisao_humana`.
- **Regra de ouro**: `manter_no_kit` só sai da regra determinística. Testado.

**(b) `foto_sem_cpf_vale(cliente, trecho_ocr, tipo_arquivo)`** — saídas só `quarentena` (padrão) | `revisao_humana` (JEV ≥ 0,85 e `provavelmente_do_cliente`). Confiança baixa mantém quarentena (JEV não acrescentou informação). Nunca aceita.

**(c) `lote_misturado_acao(resumo_lote)`** — choice {separar, pedir_reenvio, revisao_humana}. Regra atual = `revisao_humana`. JEV < 0,85 → revisão; `separar` só se `n_documentos_sem_dono == 0` (senão revisão). State só leva contagens/tipos (`CAMPOS_RESUMO_LOTE`); campos extras são descartados.

Todo roteador devolve `{acao, motivo, confianca, fonte}`; falha/exceção/chave ausente ⇒ regra com `fonte="falha_jev"`, sem exceção.

## PII — trade-off
- CPF cru **nunca** vai ao JEV: o código manda o fato `cpf_do_documento_igual_ao_do_cliente` (sim/nao/ambiguo/ausente). O trecho OCR vai truncado e por `seguranca.mascarar` (CPF → `***.456.789-**`, senha removida).
- Custo: o JEV não consegue reconciliar CPF com 1 dígito lido errado. Aceito: CPF com DV inválido não é identidade (C2b) — vai para humano.
- O **nome** do cliente vai (é o objeto do julgamento) e o OCR pode conter endereço/NB. Mitigação: truncagem; avaliar contrato de tratamento de dados com a TypeSafe antes de `decide`.
- JSONL: registro inteiro passa por `seguranca.mascarar`; não grava o state nem o OCR — só ids, ação da regra, ação/escolha/confiança do JEV, latência, tokens, `concorda`.

## Modo sombra
`typesafe.ativo=true, modo="sombra"`: chama o JEV, grava JSONL, **devolve a regra**. Métrica para ligar `decide`: taxa de `concorda`, distribuição de confiança, casos `divergencia_jev`. Sugestão: ≥ 2 semanas e revisão manual das divergências antes de `decide`.

## Encaixe proposto em `lote_para_fila.py` (NÃO plugado)
A versão do servidor tem bloco OCR (`ocr_documento.py`) ausente aqui. Ponto: **depois** do OCR / `separar_por_conteudo` e **antes** de montar o plano/enfileirar (`planejar` → `gravar_na_fila`), por documento do lote:
```python
import decisoes_jev
d = decisoes_jev.documento_e_do_cliente({"nome": lote["nome"], "cpf": lote["cpf"]}, texto_ocr)
if d["acao"] != "manter_no_kit":
    # tirar do staging: quarentena -> pasta de quarentena; revisao_humana -> estado_lote.marcar_revisao(lote, "JEV_"+d["acao"])
# sem CPF:   decisoes_jev.foto_sem_cpf_vale(cliente, texto_ocr, tipo_arquivo)
# lote com >1 dono: decisoes_jev.lote_misturado_acao(resumo)  # resumo = contagens; "separar" -> dividir_lote_misto
```
Com `modo` ≠ `decide` o retorno é idêntico ao comportamento atual, então o encaixe pode ir para produção em sombra sem mudar resultado.

## Evidência (saídas reais, 09/11/2026)
```
$ .venv/bin/python -m pyflakes typesafe_cliente.py decisoes_jev.py seguranca.py testes/test_typesafe_cliente.py testes/test_decisoes_jev.py testes/test_seguranca.py
pyflakes rc=0
$ .venv/bin/python -m pytest -q testes/test_typesafe_cliente.py testes/test_decisoes_jev.py testes/test_seguranca.py
251 passed in 1.21s
$ .venv/bin/python -m pytest -q
1282 passed, 2 skipped, 1 xfailed in 17.31s
```
Nenhuma chamada real à API (transporte falso; conftest bloqueia socket). Chave de teste fictícia `apikey_teste_...`.

Cobertura: shape válido/inválido (choice com lista, sem type, score 1/11 níveis/objeto, box, noul sem instructions/criteria errado) sem tocar transporte; 401 sem retry; 429/529/503 com retry e recuperação; Retry-After com teto 5 s; esgotamento; timeout/reset; 422 `dict_type` sem ecoar state/CPF; resposta incompleta/não-JSON; desligado por padrão (config do repo não liga); chave fora de caplog/str/repr; chave por arquivo principal/key_file; key_file 0644 + estrito ⇒ SegredoRecusado; falha (6 tipos × sombra/decide × 3 decisões) devolve regra com `falha_jev`; sombra devolve a regra e grava JSONL 0600 sem CPF/OCR; decide com confiança baixa ⇒ revisão; JEV nunca promove dono desconhecido; foto sem CPF nunca aceita; lote `separar` bloqueado com doc sem dono; state sem CPF cru.

## Pendências para outras frentes / servidor
- **Orquestrador (config.json)**: acrescentar o bloco `typesafe` (ativo=false) quando quiser expor a opção; sem ele o padrão já é desligado.
- **Servidor**: mover a chave para `/etc/triagem-bitrix/typesafe_key` (0600 root) — hoje lida como legado `/opt/triagem-bitrix/typesafe.key`/`/root/.typesafe-jev.key` com aviso (com `TRIAGEM_SEGREDOS_ESTRITO=1` e permissão aberta, recusa).
- **Dono de `lote_para_fila.py`** (com `ocr_documento.py` do servidor): plugar no ponto descrito acima, em `modo=sombra` primeiro.
- **CONTRATOS.md (Z3)**: registrar `typesafe_cliente`/`decisoes_jev` e a máscara `apikey_`, e o nome de segredo `typesafe_key`.

## Riscos
- Uso de `seguranca._ler_arquivo` (privado) para `key_file`; se o contrato quiser, promover a pública.
- Limiares (0,85 / risco 1,5) são ponto de partida, não calibrados: calibrar com o JSONL da sombra.
- Opções `indefinido`/`sem_elementos` diluem a confiança (doc recomenda Noul companheiro) — mais revisões humanas, nunca menos segurança.
