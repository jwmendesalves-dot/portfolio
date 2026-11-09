# R3 — correções da revisão final 3 (segurança/JEV, A1..A8)

Status: **CONCLUÍDO** (sem commit — sem git de escrita nesta frente).

Base: `docs/remediacao/revisao/final-3-seguranca.md`. Retomada de subagente interrompido
(edições já estavam no disco); completada e testada aqui.

## Correções
| # | Arquivo | O que mudou |
|---|---|---|
| A1 | `seguranca.py` | `_RX_CPF` aceita `/` ou espaço antes do DV e espaço entre grupos (`000.000.000/00`, `000.000.000 00`, `000 000 000 00`). Anti falso positivo: com `/`/espaço só mascara se os grupos forem pontuados ou o DV for válido (telefone/processo/contagens/IP passam intactos). `decisoes_jev._trecho_seguro` herda. |
| A2 | `decisoes_jev.py` | `_conf`: só int/float (bool não), finito, em [0,1]; resto → 0.0 (abaixo do limiar → revisão humana/padrão). `_score`: finito em [0, n_niveis-1]; inválido conta como **risco alto** (nunca promove ao kit). Aplicado em documento, foto e lote. |
| A3 | `typesafe_cliente.py`, `seguranca.py` | `ler_chave` recusa chave com espaço/controle (`SegredoInvalido`, sem valor). `_chamar` converte `ValueError` do transporte em `ErroRequisicao` genérico, levantado fora do `except` (sem `__cause__`/`__context__` com o Bearer). `mascarar` cobre `Bearer <v>` solto. |
| A4 | `seguranca.py` | `api_key`, `apikey`, `typesafe_key` em `_PARAMS_LIVRES` (forma `nome=v` e `"nome": "v"`). |
| A5 | `typesafe_cliente.py` | `_resumo_erro` só emite códigos de `CODIGOS_ERRO_CONHECIDOS` (resto `<detalhe omitido>`); `loc` só segmentos `[a-z_]{1,40}`/int (resto `?`); trata `{"error": {"type": ...}}`. |
| A6 | `telegram_grupo.py` (`ler_token`) | `seguranca.ler_segredo("telegram_token")` (env `TRIAGEM_TELEGRAM_TOKEN` → `SEGREDOS_DIR` → legado `/root/.telegram/documentosexemplo.token`), com checagem 0600/dono, recusa /tmp e modo estrito. `TELEGRAM_TOKEN_FILE` continua valendo, lido por `seguranca._ler_arquivo` (mesmas checagens). Ausente → `SegredoAusente` sem valor. |
| A7 | `decisoes_jev.py` | `registrar_jsonl`: NaN/inf → `null` recursivo, `allow_nan=False`. |
| A8 | `certidoes.py` | stdout/stderr do node mascarados **antes** de truncar em 80 chars. |

## Testes
- Novo: `testes/test_r3_revfinal_seguranca.py` — 43 testes, dados sintéticos
  (`cpf_util.gerar_para_teste`, chaves inventadas), inclui falsos positivos de A1 e
  caminho legado/estrito de A6.
- Prova contra HEAD (cópia `git archive HEAD` em tmp + o arquivo novo):
  **27 failed, 16 passed** — todos os A1..A8 têm ao menos um teste falhando no HEAD
  (A1×5, A2×5, A3×5, A4×4, A5×3, A6×3, A7×1, A8×1). Os 16 que passam no HEAD são
  controles (falso positivo, código conhecido preservado, legado, confiança válida).
- Adversarial do revisor (`~/.hermes/cache/scratch/revfinal3/test_adversarial.py`):
  9 falhas no HEAD → **25 passed**.

```
$ .venv/bin/python -m pyflakes seguranca.py decisoes_jev.py typesafe_cliente.py telegram_grupo.py certidoes.py testes/test_r3_revfinal_seguranca.py
(sem saída; exit 0)
$ .venv/bin/python -m pytest -q
1332 passed, 2 skipped, 1 xfailed in 21.16s
```

## Observações
- A6 usa `seguranca._ler_arquivo` (privado) para o caminho extra `TELEGRAM_TOKEN_FILE`,
  mesmo padrão já adotado por `typesafe_cliente.ler_chave` para `key_file`.
- Nenhum CPF/segredo real impresso; nenhum CPF existente alterado. Arquivos de outras
  frentes (completar_cpf_por_nome, ponte_criterio, download_svc, deploy/**) não tocados.
