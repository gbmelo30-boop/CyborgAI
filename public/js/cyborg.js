// ==============================================================================
// COMUNICAÇÃO COM O BACKEND E SALVAMENTO (LLaMA + RAG)
// ==============================================================================
const CYBORG = {
    init: () => { window.systemLog("Cyborg AI (LLaMA): Conectado via Fetch."); },

    delay: (ms) => new Promise(resolve => setTimeout(resolve, ms)),

    enviarMensagem: async (textoUsuario, sessionId = null, signal = null) => {
        if (!DB.user) {
            window.systemLog("Usuário não identificado", "ERRO");
            return { response: "Erro: Você precisa estar identificado para conversar.", error: true };
        }

        await CYBORG.delay(300);
        let currentSessionId = sessionId;

        // Cria a sessão no início de uma nova conversa
        if (!currentSessionId) {
            const sessao = await DB.criarSessao(textoUsuario);
            if (sessao) currentSessionId = sessao.id;
            else return null;
        }

        try {
            // 1. Salva a pergunta do usuário
            await DB.salvarMensagem(currentSessionId, "user", textoUsuario);

            // 2. Monta o histórico — fonte PRIMARIA: memória em RAM da sessão.
            // Assim o chat funciona com contexto completo mesmo com a gravação no
            // banco DESLIGADA (o banco passa a ser apenas persistência, nunca dependência).
            if (!window.__memHist) window.__memHist = {};
            const memHist = window.__memHist[currentSessionId] || [];
            const historicoRaw = await DB.carregarHistorico(currentSessionId);
            const doBanco = historicoRaw.map(msg => ({
                role: msg.role === 'assistant' ? 'assistant' : 'user',
                content: msg.content
            }));
            // usa o que tiver mais contexto (banco vazio/curto por estar off -> usa a RAM)
            let historyForAI = (doBanco.length >= memHist.length) ? doBanco : memHist.slice();
            // a pergunta atual SEMPRE vai no payload (o chat nunca envia vazio -> nunca da 400)
            const ultima = historyForAI[historyForAI.length - 1];
            if (!ultima || ultima.role !== 'user' || ultima.content !== textoUsuario) {
                historyForAI.push({ role: 'user', content: textoUsuario });
            }
            window.__memHist[currentSessionId] = historyForAI.slice();

            const contextData = window.currentResearchContext ||
                                JSON.parse(localStorage.getItem('cyborg_current_session')) ||
                                { group: 'Individual/Visitante', topic: 'Geral' };
            const temaAtual = contextData.topic || 'Geral';

            window.systemLog(`Solicitando resposta (Tema: ${temaAtual} | RAG: ${window.useRag})`);

            if (!API_BASE_URL || API_BASE_URL.includes("COLE_O_LINK")) {
                throw new Error("Link da API não configurado.");
            }

            const response = await fetch(`${API_BASE_URL}/api/chat`, {
                method: 'POST',
                signal: signal || undefined,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    messages: historyForAI,
                    tema: temaAtual,
                    grupo: contextData.group,
                    use_rag: window.useRag,
                    idioma: window.currentLang || 'pt',
                    session_id: currentSessionId,
                    userName: (contextData.userName || ''),
                    user_id: DB.user.id,
                    estilo: (localStorage.getItem('cyborg_estilo') || 'equilibrado'),
                    modelo: (localStorage.getItem('cyborg_modelo') || 'local')
                })
            });

            if (!response.ok) {
                const errData = await response.json().catch(() => ({}));
                let code = 'erro_generico';
                if (response.status === 503) code = 'erro_carregando';
                else if (response.status >= 500) code = 'erro_servidor';
                else if (response.status === 401 || response.status === 403) code = 'erro_permissao';
                else if (response.status >= 400) code = 'erro_requisicao';
                window.systemLog(`Falha HTTP ${response.status}: ${errData.error || ''}`, "ERRO");
                return { error: errData.error || ('HTTP ' + response.status), errorCode: code };
            }

            const result = await response.json();
            if (result.error) throw new Error(result.error);

            // Memoria automatica: quando o backend sinaliza, atualiza o perfil em segundo plano
            if (result.memory_should_refresh && DB.atualizarMemoria && DB.user) {
                DB.atualizarMemoria().catch(() => {});
            }

            const text = (result.response || "").replace("<<FIM>>", "").trim();

            // guarda a resposta na memória da sessão (contexto persiste mesmo sem gravar no banco)
            (window.__memHist[currentSessionId] = window.__memHist[currentSessionId] || []).push({ role: 'assistant', content: text });

            // 3. Salva a resposta da IA (registrando se o RAG foi usado, o estilo e o modelo)
            const estiloUsado = (localStorage.getItem('cyborg_estilo') || 'equilibrado');
            const modeloUsado = (result.modelo || localStorage.getItem('cyborg_modelo') || 'local');
            await DB.salvarMensagem(currentSessionId, "assistant", text, !!result.used_rag, estiloUsado, modeloUsado);
            window.systemLog("Resposta salva.");

            return { response: text, sessionId: currentSessionId };

        } catch (e) {
            if (e && (e.name === 'AbortError' || (signal && signal.aborted))) {
                window.systemLog("Geração interrompida pelo usuário.");
                return { aborted: true };
            }
            console.error("ERRO GERAL:", e);
            let code = 'erro_generico';
            const msg = (e && e.message) || '';
            if (typeof navigator !== 'undefined' && navigator.onLine === false) code = 'erro_offline';
            else if (e instanceof TypeError || /failed to fetch|networkerror|load failed|network request failed/i.test(msg)) code = 'erro_servidor_inacessivel';
            return { error: msg, errorCode: code };
        }
    }
};

CYBORG.init();
window.CYBORG = CYBORG;
