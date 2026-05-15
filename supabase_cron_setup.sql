-- =========================================================================
-- CONFIGURACAO DO pg_cron PARA LIMPEZA AUTOMATICA DE PROCESSOS EXPIRADOS
-- LGPD art. 16: eliminacao dos dados apos o termino do tratamento
--
-- PASSOS OBRIGATORIOS:
-- 1. Supabase Dashboard -> Database -> Extensions -> Ativar "pg_cron"
-- 2. Execute este script no SQL Editor do Supabase
-- =========================================================================


-- =========================================================================
-- PASSO 1: Verificar se pg_cron esta ativo
-- (deve retornar uma linha com extname = 'pg_cron')
-- =========================================================================
SELECT extname, extversion
FROM pg_extension
WHERE extname = 'pg_cron';


-- =========================================================================
-- PASSO 2: Agendar limpeza diaria de processos expirados (3h UTC)
-- Executa cleanup_expired_processes() que deleta processos com
-- expires_at < now() (730 dias apos criacao, conforme LGPD art. 16).
-- =========================================================================
SELECT cron.schedule(
    'defensor-ia-cleanup-expired',        -- nome unico do job
    '0 3 * * *',                          -- todos os dias as 3h UTC (0h BRT / 0h BRT-3)
    $$SELECT cleanup_expired_processes()$$
);


-- =========================================================================
-- PASSO 3: Verificar que o job foi criado corretamente
-- =========================================================================
SELECT
    jobid,
    jobname,
    schedule,
    command,
    active
FROM cron.job
WHERE jobname = 'defensor-ia-cleanup-expired';


-- =========================================================================
-- PASSO 4 (OPCIONAL): Agendar log semanal do numero de processos ativos
-- Util para monitorar o crescimento da base de dados.
-- =========================================================================
-- SELECT cron.schedule(
--     'defensor-ia-weekly-count',
--     '0 8 * * 1',   -- toda segunda-feira as 8h UTC
--     $$
--     INSERT INTO data_access_log (user_id, action, occurred_at)
--     SELECT
--         '00000000-0000-0000-0000-000000000000'::uuid,
--         'system_weekly_count_' || COUNT(*)::text,
--         now()
--     FROM processes;
--     $$
-- );


-- =========================================================================
-- COMANDOS DE MANUTENCAO
-- =========================================================================

-- Desativar temporariamente o job sem deletar:
-- UPDATE cron.job SET active = false WHERE jobname = 'defensor-ia-cleanup-expired';

-- Reativar:
-- UPDATE cron.job SET active = true WHERE jobname = 'defensor-ia-cleanup-expired';

-- Remover o job permanentemente:
-- SELECT cron.unschedule('defensor-ia-cleanup-expired');

-- Ver historico de execucoes (ultimas 10):
-- SELECT
--     runid,
--     job_pid,
--     database,
--     username,
--     status,
--     return_message,
--     start_time,
--     end_time
-- FROM cron.job_run_details
-- WHERE job_id = (SELECT jobid FROM cron.job WHERE jobname = 'defensor-ia-cleanup-expired')
-- ORDER BY start_time DESC
-- LIMIT 10;


-- =========================================================================
-- TESTE MANUAL: Executar a limpeza agora (sem esperar o cron)
-- =========================================================================
-- SELECT cleanup_expired_processes();
-- Retorna: numero de processos deletados (0 se nenhum expirou ainda)


-- =========================================================================
-- FIM DO SCRIPT
-- =========================================================================
