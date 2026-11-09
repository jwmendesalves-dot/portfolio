"""Scripts de manutenção pontual (reparos de um caso só). Não rodam em timer.

Todos: simulação por padrão, gravação só com `--aplicar --confirmar`, idempotentes,
estado gravado via estado_store (trava + atômico) e backup via estado_store.fazer_backup.
"""
