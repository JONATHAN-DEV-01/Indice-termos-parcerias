/**
 * busca.js — Motor de busca client-side para o Portal de Parcerias
 *
 * Como funciona:
 *  1. Ao iniciar, faz fetch() do indice-busca.json e mantém em memória.
 *  2. Ouve digitações no campo de busca (debounce 200ms).
 *  3. Normaliza e filtra; match no título tem peso 2×, no conteúdo 1×.
 *  4. Gera snippet de ~150 chars ao redor do primeiro match no conteúdo.
 *  5. Destaca o termo buscado com <mark>.
 *  6. Renderiza cards acessíveis na lista de resultados.
 *
 * Configuração:
 *  - Altere INDEX_URL para o caminho real do seu JSON no servidor.
 *  - SNIPPET_RADIUS controla o tamanho do trecho de contexto.
 *  - MAX_RESULTS controla o número máximo de resultados exibidos.
 */

(function () {
  "use strict";

  /* ────────────────────────────────────────────
     Configurações
     ──────────────────────────────────────────── */
  var INDEX_URL     = "/indice-busca.json"; // ajuste conforme o servidor
  var DEBOUNCE_MS   = 200;
  var SNIPPET_RADIUS = 90;  // caracteres em cada lado do match
  var MAX_RESULTS   = 30;

  /* ────────────────────────────────────────────
     Referências ao DOM
     ──────────────────────────────────────────── */
  var elRoot         = document.getElementById("portal-busca-root");
  var elInput        = document.getElementById("pb-search-input");
  var elClear        = document.getElementById("pb-search-clear");
  var elLoading      = document.getElementById("pb-state-loading");
  var elError        = document.getElementById("pb-state-error");
  var elPrompt       = document.getElementById("pb-state-prompt");
  var elEmpty        = document.getElementById("pb-state-empty");
  var elEmptyMsg     = elEmpty ? elEmpty.querySelector("strong") : null;
  var elCount        = document.getElementById("pb-results-count");
  var elList         = document.getElementById("pb-results-list");

  if (!elRoot || !elInput || !elList) {
    console.warn("[busca.js] Elementos do DOM não encontrados. Verifique o HTML.");
    return;
  }

  /* ────────────────────────────────────────────
     Estado interno
     ──────────────────────────────────────────── */
  var indice   = [];     // array de { titulo, url, conteudo }
  var carregado = false;
  var timerDebounce = null;

  /* ────────────────────────────────────────────
     Utilitários
     ──────────────────────────────────────────── */

  /** Normaliza string: minúsculas + remove acentos NFD */
  function normalizar(str) {
    return (str || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  /** Escapa caracteres HTML para evitar XSS */
  function escapeHtml(str) {
    return (str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** Escapa regex special chars */
  function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /** Mostra/oculta um elemento (usando o atributo hidden) */
  function mostrar(el) { if (el) el.hidden = false; }
  function ocultar(el) { if (el) el.hidden = true;  }

  /** Exibe somente um dos painéis de estado */
  function mostrarEstado(estado) {
    ocultar(elLoading);
    ocultar(elError);
    ocultar(elPrompt);
    ocultar(elEmpty);
    ocultar(elCount);
    if (estado === "loading") mostrar(elLoading);
    if (estado === "error")   mostrar(elError);
    if (estado === "prompt")  mostrar(elPrompt);
    if (estado === "empty")   mostrar(elEmpty);
  }

  /* ────────────────────────────────────────────
     Carregamento do índice JSON
     ──────────────────────────────────────────── */
  function carregarIndice() {
    mostrarEstado("loading");

    fetch(INDEX_URL)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!Array.isArray(data)) throw new Error("JSON inválido: esperado array");
        indice = data;
        carregado = true;
        mostrarEstado("prompt");
        elInput.disabled = false;
        elInput.focus();
      })
      .catch(function (err) {
        console.error("[busca.js] Falha ao carregar índice:", err);
        mostrarEstado("error");
      });
  }

  /* ────────────────────────────────────────────
     Motor de busca e ranking
     ──────────────────────────────────────────── */

  /**
   * Gera snippet de contexto ao redor do primeiro match.
   * @returns {string} trecho com "..." nas bordas e <mark> no match
   */
  function gerarSnippet(conteudo, termoRaw) {
    if (!conteudo) return "";

    var termoNorm  = normalizar(termoRaw);
    var conteudoN  = normalizar(conteudo);
    var posMatch   = conteudoN.indexOf(termoNorm);

    if (posMatch === -1) {
      // Fallback: retornar os primeiros chars
      return escapeHtml(conteudo.slice(0, SNIPPET_RADIUS * 2)) + (conteudo.length > SNIPPET_RADIUS * 2 ? "…" : "");
    }

    var inicio = Math.max(0, posMatch - SNIPPET_RADIUS);
    var fim    = Math.min(conteudo.length, posMatch + termoRaw.length + SNIPPET_RADIUS);

    var trecho = (inicio > 0 ? "…" : "") +
                  escapeHtml(conteudo.slice(inicio, fim)) +
                  (fim < conteudo.length ? "…" : "");

    // Destacar o termo (case-insensitive, sem acentos — via posição calculada)
    var reDestaque = new RegExp("(" + escapeRegex(escapeHtml(termoRaw)) + ")", "gi");
    trecho = trecho.replace(reDestaque, "<mark>$1</mark>");

    return trecho;
  }

  /** Destaca ocorrências no título */
  function destacarTitulo(titulo, termoRaw) {
    var tituloEsc = escapeHtml(titulo);
    var reDestaque = new RegExp("(" + escapeRegex(escapeHtml(termoRaw)) + ")", "gi");
    return tituloEsc.replace(reDestaque, "<mark>$1</mark>");
  }

  /**
   * Busca e ordena resultados.
   * Score: match no título = 2, match no conteúdo = 1.
   * Múltiplos termos: todos os termos devem aparecer (AND implícito).
   */
  function buscar(query) {
    if (!query.trim()) return [];

    var termos = normalizar(query).split(/\s+/).filter(Boolean);

    var resultados = [];

    for (var i = 0; i < indice.length; i++) {
      var item       = indice[i];
      var tituloN    = normalizar(item.titulo);
      var conteudoN  = normalizar(item.conteudo);
      var score      = 0;
      var todosMatch = true;

      for (var t = 0; t < termos.length; t++) {
        var termo = termos[t];
        var noTitulo   = tituloN.indexOf(termo) !== -1;
        var noConteudo = conteudoN.indexOf(termo) !== -1;

        if (!noTitulo && !noConteudo) {
          todosMatch = false;
          break;
        }

        if (noTitulo)   score += 2;
        if (noConteudo) score += 1;
      }

      if (todosMatch && score > 0) {
        resultados.push({ item: item, score: score });
      }
    }

    // Ordenar por score decrescente
    resultados.sort(function (a, b) { return b.score - a.score; });

    return resultados.slice(0, MAX_RESULTS);
  }

  /* ────────────────────────────────────────────
     Renderização dos cards
     ──────────────────────────────────────────── */
  function renderizar(resultados, queryRaw) {
    elList.innerHTML = "";

    if (resultados.length === 0) {
      if (elEmptyMsg) elEmptyMsg.textContent = queryRaw;
      mostrarEstado("empty");
      return;
    }

    // Mostrar contador
    mostrar(elCount);
    elCount.textContent =
      resultados.length + (resultados.length === 1 ? " resultado" : " resultados") +
      " para \"" + queryRaw + "\"";

    // Ocultar outros estados
    ocultar(elLoading);
    ocultar(elError);
    ocultar(elPrompt);
    ocultar(elEmpty);

    // Termo principal para snippet (usa a query bruta completa)
    var termoSnippet = queryRaw.trim();

    var fragment = document.createDocumentFragment();

    for (var i = 0; i < resultados.length; i++) {
      var res  = resultados[i];
      var item = res.item;

      var li = document.createElement("li");
      li.className = "pb-result-card";
      li.setAttribute("role", "listitem");

      var matchTitulo = normalizar(item.titulo).indexOf(normalizar(termoSnippet)) !== -1;

      var snippet  = gerarSnippet(item.conteudo, termoSnippet);
      var titDestq = destacarTitulo(item.titulo, termoSnippet);
      var urlSegura = escapeHtml(item.url);
      var urlDisplay = urlSegura.replace(/^https?:\/\//, "");

      li.innerHTML =
        '<a class="pb-result-title" href="' + urlSegura + '" target="_blank" rel="noopener">' +
          titDestq +
          (matchTitulo ? '<span class="pb-badge-title-match">título</span>' : "") +
        '</a>' +
        '<span class="pb-result-url">' + urlDisplay + '</span>' +
        '<p class="pb-result-snippet">' + snippet + '</p>';

      fragment.appendChild(li);
    }

    elList.appendChild(fragment);
  }

  /* ────────────────────────────────────────────
     Handler de busca (com debounce)
     ──────────────────────────────────────────── */
  function handleInput() {
    clearTimeout(timerDebounce);
    timerDebounce = setTimeout(function () {
      var query = elInput.value;

      // Botão limpar
      if (elClear) elClear.hidden = !query;

      if (!carregado) return;

      if (!query.trim()) {
        elList.innerHTML = "";
        mostrarEstado("prompt");
        return;
      }

      var resultados = buscar(query);
      renderizar(resultados, query);

    }, DEBOUNCE_MS);
  }

  function handleClear() {
    elInput.value = "";
    elInput.focus();
    elList.innerHTML = "";
    if (elClear) elClear.hidden = true;
    mostrarEstado("prompt");
  }

  /* ────────────────────────────────────────────
     Inicialização
     ──────────────────────────────────────────── */
  elInput.disabled = true; // desabilitado até o JSON carregar
  elInput.addEventListener("input", handleInput);
  if (elClear) elClear.addEventListener("click", handleClear);

  // Suporte à tecla Escape para limpar
  elInput.addEventListener("keydown", function (e) {
    if (e.key === "Escape") handleClear();
  });

  carregarIndice();

})();
