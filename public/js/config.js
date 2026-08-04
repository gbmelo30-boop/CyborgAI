// ==============================================================================
// CONFIGURAÇÃO DA API
// ------------------------------------------------------------------------------
// O backend (/api/) é servido pelo MESMO host que serve o app (o nginx faz o
// proxy). Por isso usamos a própria origem da página: assim o app funciona em
// HTTP ou HTTPS, por IP ou por domínio, sem precisar alterar código nenhum.
// O endereço fixo abaixo é só um plano B (ex.: abrir o arquivo localmente).
// ==============================================================================
const API_FALLBACK = "https://cyborgai.duckdns.org";
const API_BASE_URL = (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin
    : API_FALLBACK;

window.systemLog = function(mensagem, tipo = "INFO") {
    const timestamp = new Date().toLocaleTimeString();
    console.log(`[${timestamp}] [${tipo}] ${mensagem}`);
}

async function iniciarSistema() {
    window.systemLog("--- Iniciando Cyborg AI (LLM local) ---");
}
iniciarSistema();

// UUID robusto: funciona também em contexto NÃO seguro (HTTP), onde
// crypto.randomUUID pode não existir. Usa getRandomValues como fallback.
window.gerarUUID = function() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) {}
    const rnd = (window.crypto && crypto.getRandomValues)
        ? () => crypto.getRandomValues(new Uint8Array(1))[0]
        : () => Math.floor(Math.random() * 256);
    return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c =>
        (c ^ (rnd() & (15 >> (c / 4)))).toString(16)
    );
};

// ---- Heartbeat para o contador de "online agora" do admin ----
(function () {
    try {
        var CID = localStorage.getItem('cyborg_cid');
        if (!CID) { CID = (window.gerarUUID ? window.gerarUUID() : String(Math.random())); localStorage.setItem('cyborg_cid', CID); }
        function nomeAtual(){ try{ var ss=JSON.parse(localStorage.getItem('cyborg_current_session')||'{}'); return ss.userName || 'Visitante'; }catch(e){ return 'Visitante'; } }
        function ping() { try { fetch(API_BASE_URL + '/api/ping', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cid: CID, nome: nomeAtual() }) }); } catch (e) {} }
        ping(); setInterval(ping, 30000);
    } catch (e) {}
})();
