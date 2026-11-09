# Regras comuns — Operação Resgate triagem-bitrix (LEIA ANTES DE TUDO)

Repositório: `~/triagem-bitrix-remediacao` (branch `remediacao`; cópia local de `/opt/triagem-bitrix`).
Auditoria (fonte dos IDs de achados): `docs/auditoria/00-RESUMO-EXECUTIVO.md` e `01`…`07`, `CONTEXTO-TRIAGEM.md`, `BITRIX-ESCRITORIO.md`.
Contratos compartilhados: `docs/remediacao/CONTRATOS.md` (assinaturas que TODOS usam — não crie mecanismo paralelo de trava, CPF, máscara, erro ou rate limit).
Doc oficial Bitrix24 local: `~/Documents/bitrix24-docs/` (consulte antes de assumir assinatura de método REST).

## Proibido
- Comandos git de escrita (`add`, `commit`, `checkout`, `reset`, `stash`, `rm`). Só o orquestrador comita. `git diff`/`git status`/`git log` podem.
- Editar arquivo que não é seu (lista de donos abaixo / no seu briefing). Precisa de mudança em arquivo alheio → descreva em "Pendências para outras frentes" no seu relatório.
- Rede: nenhuma chamada a Bitrix, VibeCode, Telegram, Google. Os testes já bloqueiam socket (testes/conftest.py).
- Dados reais: não existe `estado.json` real aqui e não é para criar um com dados de cliente. Fixtures sintéticas, CPFs de teste gerados (DV válido calculado), nomes fictícios.
- Imprimir/registrar segredo, CPF real ou senha. Há credencial e CPFs reais no código legado: ao removê-los, não os copie para relatório, teste ou comentário.
- Dependência nova (pip). Só stdlib + o que já é importado (requests). Python **3.9** compatível: `from __future__ import annotations` em módulo com anotações `X | None`; nada de `match`.
- Apagar dados/backups ou mudar comportamento de produção "silenciosamente": toda mudança de comportamento vai no relatório.

## Obrigatório
- Corrigir a CAUSA, com teste que reproduz o defeito antigo (falha no código original) e passa depois. Caminho normal + caminho de falha.
- Edição pontual (`patch`), relendo o arquivo do disco antes de cada edição. Nunca sobrescrever um arquivo inteiro com cópia antiga.
- Testes: `cd ~/triagem-bitrix-remediacao && .venv/bin/python -m pytest -q testes/<seus_arquivos>`; antes de terminar rode a suíte inteira `.venv/bin/python -m pytest -q` (leva ~2 s) e reporte o número real. Falhas que não são suas: liste, não conserte.
- Caminhos de produção: use `caminhos.BASE` / `caminhos.SEPARADOR` (ver CONTRATOS) em vez de `/opt/...` literal nos arquivos que você tocar.
- Preservar contratos externos: formato de `fila-jobs.json`/job do painel, nomes de status já gravados em `estado.json` (mapeie, não renomeie), CLI flags existentes (pode adicionar; remover só se perigosa, com aviso).
- Relatório em `docs/remediacao/frentes/<SUA-FRENTE>.md`, gravado cedo e atualizado a cada passo (você pode ser interrompido em ~25 min):
  - por achado: `ID` · status (corrigido / parcial / bloqueado / não aplicável) · arquivos · o que mudou · teste(s) · evidência (comando + resultado real);
  - achado que você reconfirmou não existir mais ou ser diferente do relatório: diga;
  - pendências para outras frentes; riscos; o que precisa do servidor.
- Resposta final ao orquestrador: caminho do relatório, lista exata de arquivos alterados/criados, resultado real da suíte, pendências.

## Donos de arquivo (onda atual no seu briefing; arquivo fora da lista = não toque)
Arquivos sensíveis com dono único por onda: `triagem_grupo.py`, `estado_store.py`, `lote_para_fila.py`, `lote_para_job.py`, `config.json`, `pipeline.sh`, `telegram_grupo.py`, `testes/conftest.py` (orquestrador), `caminhos.py` (orquestrador).
