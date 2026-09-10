/**
 * Portal Busca — Script de Busca por Índice JSON
 * Versão: 2.0 (corrigida para carregamento externo)
 *
 * INSTRUÇÕES DE USO:
 * 1. Faça upload deste arquivo para o servidor WordPress
 *    (ex: /wp-content/uploads/portal-busca.js)
 * 2. No Elementor, adicione um widget HTML com apenas:
 *    <script src="URL_COMPLETA_DO_ARQUIVO/portal-busca.js"></script>
 *    OU enfileire via functions.php (veja comentário ao final)
 *
 * IMPORTANTE: NÃO cole este código no campo "Simple Custom CSS and JS".
 * O WordPress escapa entidades HTML (&, <, >, ") dentro desse campo,
 * corrompendo a sintaxe do JavaScript e causando SyntaxError.
 */

document.addEventListener("DOMContentLoaded", function () {
  "use strict";

  /* ────────────────────────────────────────────
     1. Configurações Iniciais
     ──────────────────────────────────────────── */
  var URL_DO_JSON    = "https://hom-portaldeparcerias.prefeitura.sp.gov.br/wp-content/uploads/2026/09/indice-busca.json";
  var SNIPPET_RADIUS = 90;
  var MAX_RESULTS    = 30;

  /* ────────────────────────────────────────────
     2. Referências ao DOM
     ──────────────────────────────────────────── */
  var elRoot    = document.getElementById("portal-busca-root");
  var elInput   = document.getElementById("pb-search-input");
  var elLoading = document.getElementById("pb-state-loading");
  var elError   = document.getElementById("pb-state-error");
  var elPrompt  = document.getElementById("pb-state-prompt");
  var elEmpty   = document.getElementById("pb-state-empty");
  var elCount   = document.getElementById("pb-results-count");
  var elList    = document.getElementById("pb-results-list");

  if (!elRoot || !elList) {
    console.warn("Portal Busca: Elementos do DOM nao encontrados. Verifique o widget HTML no Elementor.");
    return;
  }

  elList.classList.add("pb-grid");

  var indice = [];

  /* ────────────────────────────────────────────
     3. Funções Utilitárias
     ──────────────────────────────────────────── */

  var MARCAS_COMBINANTES_RE = new RegExp(
    "[" + String.fromCharCode(0x0300) + "-" + String.fromCharCode(0x036f) + "]",
    "g"
  );

  function normalizar(str) {
    return (str || "").toLowerCase().normalize("NFD").replace(MARCAS_COMBINANTES_RE, "");
  }

  /**
   * CORREÇÃO PRINCIPAL: usa document.createTextNode para escapar HTML.
   * Isso evita que o código contenha as strings literais &amp; &lt; &gt; &quot;
   * que o WordPress re-escapa ao salvar no campo de Custom CSS/JS,
   * causando o SyntaxError "missing ) after argument list".
   */
  function escapeHtml(str) {
    if (!str) return "";
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  function mostrarEstado(estado) {
    if (elLoading) elLoading.hidden = true;
    if (elError)   elError.hidden   = true;
    if (elPrompt)  elPrompt.hidden  = true;
    if (elEmpty)   elEmpty.hidden   = true;
    if (elCount)   elCount.hidden   = true;

    if (estado === "loading" && elLoading) elLoading.hidden = false;
    if (estado === "error"   && elError)   elError.hidden   = false;
    if (estado === "prompt"  && elPrompt)  elPrompt.hidden  = false;
    if (estado === "empty"   && elEmpty)   elEmpty.hidden   = false;
  }

  function mensagemVazia(texto) {
    return "<p style=\"text-align:center; font-family:'Montserrat', sans-serif; font-size:18px; color:#0d3145;\">" + escapeHtml(texto) + "</p>";
  }

  /* ────────────────────────────────────────────
     4. Carregamento do Índice e Leitura da URL
     ──────────────────────────────────────────── */
  function carregarIndice() {
    mostrarEstado("loading");

    fetch(URL_DO_JSON)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        indice = data;

        var urlParams = new URLSearchParams(window.location.search);

        if (urlParams.has("s")) {
          var termoDecodificado = (urlParams.get("s") || "").trim();

          if (elInput) elInput.value = termoDecodificado;

          if (termoDecodificado === "") {
            mostrarEstado("empty");
            if (elEmpty) elEmpty.innerHTML = mensagemVazia("Por favor, digite um termo na barra para pesquisar.");
            return;
          }

          if (termoDecodificado.length < 2) {
            mostrarEstado("empty");
            if (elEmpty) elEmpty.innerHTML = mensagemVazia("Por favor, digite pelo menos 2 caracteres.");
            return;
          }

          var resultados = buscar(termoDecodificado);
          renderizar(resultados, termoDecodificado);
        } else {
          mostrarEstado("prompt");
        }
      })
      .catch(function (erro) {
        console.error("Portal Busca: Falha ao carregar o JSON:", erro);
        mostrarEstado("error");
      });
  }

  /* ────────────────────────────────────────────
     5. Motor de Busca e Recortes
     ──────────────────────────────────────────── */

  var STOPWORDS_PT = new Set([
    "a","o","as","os","de","da","do","das","dos","e","em","um","uma","uns","umas",
    "para","com","por","que","se","ao","aos","no","na","nos","nas",
    "num","numa","nuns","numas","sao","ou","como","mais","mas","tambem",
    "isso","isto","este","esta","estes","estas","esse","essa","esses","essas",
    "seu","sua","seus","suas","pelo","pela","pelos","pelas","entre","sobre",
    "sem","ate","apos","ja","so","muito","ha"
  ]);

  function extrairTermos(query) {
    var todos = normalizar(query).split(/\s+/).filter(Boolean);
    var significativos = todos.filter(function (t) {
      return t.length > 1 && !STOPWORDS_PT.has(t);
    });
    return significativos.length > 0 ? significativos : todos;
  }

  function encontrarIntervalos(textoNorm, termoNorm) {
    var intervalos = [];
    if (!termoNorm) return intervalos;
    var idx = textoNorm.indexOf(termoNorm);
    while (idx !== -1) {
      intervalos.push([idx, idx + termoNorm.length]);
      idx = textoNorm.indexOf(termoNorm, idx + termoNorm.length);
    }
    return intervalos;
  }

  function mesclarIntervalos(intervalos) {
    if (intervalos.length === 0) return intervalos;
    var ordenados = intervalos.slice().sort(function (a, b) { return a[0] - b[0]; });
    var out = [ordenados[0].slice()];
    for (var i = 1; i < ordenados.length; i++) {
      var atual  = ordenados[i];
      var ultimo = out[out.length - 1];
      if (atual[0] <= ultimo[1]) {
        ultimo[1] = Math.max(ultimo[1], atual[1]);
      } else {
        out.push(atual.slice());
      }
    }
    return out;
  }

  function todosIntervalos(texto, termosNorm) {
    var textoNorm  = normalizar(texto);
    var intervalos = [];
    for (var i = 0; i < termosNorm.length; i++) {
      intervalos = intervalos.concat(encontrarIntervalos(textoNorm, termosNorm[i]));
    }
    return mesclarIntervalos(intervalos);
  }

  function destacarIntervalos(texto, intervalos) {
    var out    = "";
    var cursor = 0;
    for (var i = 0; i < intervalos.length; i++) {
      var s = Math.max(cursor, intervalos[i][0]);
      var e = Math.max(s, intervalos[i][1]);
      out += escapeHtml(texto.slice(cursor, s));
      out += "<span class=\"termo-destacado\">" + escapeHtml(texto.slice(s, e)) + "</span>";
      cursor = e;
    }
    out += escapeHtml(texto.slice(cursor));
    return out;
  }

  function gerarSnippet(conteudo, termoRaw) {
    if (!conteudo) return "";

    var fraseNorm       = normalizar(termoRaw).trim();
    var termos          = extrairTermos(termoRaw);
    var fraseIntervalos = fraseNorm ? todosIntervalos(conteudo, [fraseNorm]) : [];
    var termoIntervalos = todosIntervalos(conteudo, termos);
    var ancoraIntervalos = fraseIntervalos.length > 0 ? fraseIntervalos : termoIntervalos;

    if (ancoraIntervalos.length === 0) {
      return escapeHtml(conteudo.slice(0, 130)) + "...";
    }

    var ancora = ancoraIntervalos[0];
    var inicio = Math.max(0, ancora[0] - SNIPPET_RADIUS);
    var fim    = Math.min(conteudo.length, ancora[1] + SNIPPET_RADIUS);

    var todosOsIntervalos = mesclarIntervalos(fraseIntervalos.concat(termoIntervalos));

    var janela = todosOsIntervalos
      .filter(function (iv) { return iv[0] < fim && iv[1] > inicio; })
      .map(function (iv) {
        return [Math.max(0, iv[0] - inicio), Math.min(fim - inicio, iv[1] - inicio)];
      });

    var texto = conteudo.slice(inicio, fim);
    var corpo = destacarIntervalos(texto, janela);

    return (inicio > 0 ? "... " : "") + corpo + (fim < conteudo.length ? " ..." : "");
  }

  function destacarTitulo(titulo, termoRaw) {
    var fraseNorm       = normalizar(termoRaw).trim();
    var termos          = extrairTermos(termoRaw);
    var fraseIntervalos = fraseNorm ? todosIntervalos(titulo, [fraseNorm]) : [];
    var termoIntervalos = todosIntervalos(titulo, termos);
    var intervalos      = mesclarIntervalos(fraseIntervalos.concat(termoIntervalos));
    return destacarIntervalos(titulo || "", intervalos);
  }

  function buscar(query) {
    var fraseNorm  = normalizar(query).trim();
    var termos     = extrairTermos(query);
    var resultados = [];

    for (var i = 0; i < indice.length; i++) {
      var item      = indice[i];
      var tituloN   = normalizar(item.titulo);
      var conteudoN = normalizar(item.conteudo);
      var score     = 0;
      var todosMatch = true;

      var fraseNoTitulo   = fraseNorm.length > 0 && tituloN.indexOf(fraseNorm) !== -1;
      var fraseNoConteudo = fraseNorm.length > 0 && conteudoN.indexOf(fraseNorm) !== -1;

      if (fraseNoTitulo)   score += 50;
      if (fraseNoConteudo) score += 25;

      for (var t = 0; t < termos.length; t++) {
        var termo      = termos[t];
        var noTitulo   = tituloN.indexOf(termo) !== -1;
        var noConteudo = conteudoN.indexOf(termo) !== -1;

        if (!noTitulo && !noConteudo) {
          todosMatch = false;
          break;
        }
        if (noTitulo)   score += 2;
        if (noConteudo) score += 1;
      }

      if ((todosMatch || fraseNoTitulo || fraseNoConteudo) && score > 0) {
        resultados.push({ item: item, score: score });
      }
    }

    resultados.sort(function (a, b) { return b.score - a.score; });
    return resultados.slice(0, MAX_RESULTS);
  }

  /* ────────────────────────────────────────────
     6. Renderização na Tela
     ──────────────────────────────────────────── */
  function renderizar(resultados, queryRaw) {
    elList.innerHTML = "";

    if (resultados.length === 0) {
      if (elEmpty) elEmpty.innerHTML = "<p style=\"text-align:center; font-family:'Montserrat', sans-serif; font-size:18px; color:#4a5568;\">Nenhum resultado encontrado para \"<strong>" + escapeHtml(queryRaw) + "</strong>\".</p>";
      mostrarEstado("empty");
      return;
    }

    mostrarEstado("done");

    if (elCount) {
      elCount.hidden      = false;
      elCount.textContent = resultados.length + (resultados.length === 1 ? " resultado encontrado" : " resultados encontrados");
    }

    var fragment = document.createDocumentFragment();
    var VIS      = "opacity:1 !important; visibility:visible !important; animation:none !important; transform:none !important;";

    for (var i = 0; i < resultados.length; i++) {
      var item     = resultados[i].item;
      var li       = document.createElement("li");
      var snippet  = gerarSnippet(item.conteudo, queryRaw);
      var titDestq = destacarTitulo(item.titulo, queryRaw);
      var urlSeg   = escapeHtml(item.url);

      li.className = "pb-card";
      li.setAttribute("style", VIS);

      li.innerHTML =
        "<h3 class=\"pb-card-title\" style=\"" + VIS + "\">" +
          "<a href=\"" + urlSeg + "\" style=\"" + VIS + "\">" + titDestq + "</a>" +
        "</h3>" +
        "<div class=\"pb-card-excerpt\" style=\"" + VIS + "\">" +
          snippet +
        "</div>" +
        "<div class=\"pb-card-btn-wrap\" style=\"" + VIS + "\">" +
          "<div class=\"pb-card-btn\" style=\"" + VIS + "\">" +
            "<a href=\"" + urlSeg + "\" style=\"" + VIS + "\">Ler Mais</a>" +
          "</div>" +
        "</div>";

      fragment.appendChild(li);
    }

    elList.appendChild(fragment);
  }

  /* ── Inicia ── */
  carregarIndice();

});

/*
 * ══════════════════════════════════════════════════════════════
 * OPÇÃO A — Elementor Widget HTML (mais simples)
 *
 *   No Elementor, adicione um widget "HTML" com o conteúdo:
 *   <script src="https://SEU-SITE/wp-content/uploads/portal-busca.js"></script>
 *
 * ══════════════════════════════════════════════════════════════
 * OPÇÃO B — functions.php (recomendado para produção)
 *
 *   function portal_busca_enqueue() {
 *       wp_enqueue_script(
 *           'portal-busca',
 *           'https://SEU-SITE/wp-content/uploads/portal-busca.js',
 *           array(),
 *           '2.0',
 *           true  // carrega no footer, após o DOM estar pronto
 *       );
 *   }
 *   add_action('wp_enqueue_scripts', 'portal_busca_enqueue');
 *
 * ══════════════════════════════════════════════════════════════
 */

