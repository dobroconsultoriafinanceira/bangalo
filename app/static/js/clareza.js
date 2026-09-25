/* Clareza — comportamento compartilhado (sem simular backend).
 *
 * - [data-open="id"] abre <dialog id="id"> (modal); foco e Esc são do navegador,
 *   o foco volta ao acionador ao fechar.
 * - [data-close] fecha o diálogo em que está.
 * - Formulário dentro de diálogo alterado: fechar pede confirmação.
 * - form[data-busy] troca o texto do botão para "Salvando…" e impede reenvio.
 * - form[data-confirm="mensagem"] pede confirmação antes do POST.
 */
(function () {
  "use strict";

  function sujo(dialogo) {
    return Array.from(dialogo.querySelectorAll("form")).some(function (f) {
      return f.dataset.dirty === "1";
    });
  }

  function fechar(dialogo, forcar) {
    if (!forcar && sujo(dialogo) &&
        !window.confirm("Descartar alterações? Os dados preenchidos neste formulário serão perdidos.")) {
      return;
    }
    dialogo.querySelectorAll("form").forEach(function (f) { delete f.dataset.dirty; });
    dialogo.close();
  }

  document.addEventListener("click", function (ev) {
    var abrir = ev.target.closest("[data-open]");
    if (abrir) {
      var alvo = document.getElementById(abrir.dataset.open);
      if (alvo && typeof alvo.showModal === "function") {
        ev.preventDefault();
        // data-trocar: fecha o diálogo atual antes (não empilhar modais)
        if (abrir.hasAttribute("data-trocar")) {
          var atual = abrir.closest("dialog");
          if (atual && atual !== alvo) atual.close();
        }
        alvo._acionador = abrir;
        // data-reset: limpa os formulários antes (ex.: "Novo" depois de um "Editar")
        if (abrir.hasAttribute("data-reset")) {
          alvo.querySelectorAll("form").forEach(function (f) { f.reset(); delete f.dataset.dirty; });
        }
        if (abrir.dataset.titulo) {
          var h = alvo.querySelector(".dialog-head h2");
          if (h) h.textContent = abrir.dataset.titulo;
        }
        // campos pré-preenchidos a partir do acionador: data-fill-<name>="valor"
        Object.keys(abrir.dataset).forEach(function (k) {
          if (k.indexOf("fill") !== 0 || k === "fill") return;
          var nome = k.slice(4).replace(/^[A-Z]/, function (c) { return c.toLowerCase(); })
            .replace(/[A-Z]/g, function (c) { return "_" + c.toLowerCase(); });
          alvo.querySelectorAll('[name="' + nome + '"]').forEach(function (campo) {
            if (campo.type === "checkbox") campo.checked = abrir.dataset[k] === "1";
            else campo.value = abrir.dataset[k];
          });
          alvo.querySelectorAll('[data-slot="' + nome + '"]').forEach(function (el) {
            el.textContent = abrir.dataset[k];
          });
        });
        if (abrir.dataset.action) {
          var form = alvo.querySelector("form[data-dynamic-action]") || alvo.querySelector("form");
          if (form) form.action = abrir.dataset.action;
        }
        alvo.showModal();
        var foco = alvo.querySelector("[autofocus]") || alvo.querySelector(".dialog-head h2");
        if (foco) { if (!foco.hasAttribute("tabindex") && foco.tagName === "H2") foco.setAttribute("tabindex", "-1"); foco.focus(); }
      }
      return;
    }
    var fecharBtn = ev.target.closest("[data-close]");
    if (fecharBtn) {
      var dlg = fecharBtn.closest("dialog");
      if (dlg) { ev.preventDefault(); fechar(dlg); }
    }
  });

  document.addEventListener("cancel", function (ev) {
    var dlg = ev.target;
    if (dlg.tagName === "DIALOG" && sujo(dlg)) {
      ev.preventDefault();
      fechar(dlg);
    }
  }, true);

  document.addEventListener("close", function (ev) {
    var dlg = ev.target;
    if (dlg.tagName === "DIALOG" && dlg._acionador && document.contains(dlg._acionador)) {
      dlg._acionador.focus();
    }
  }, true);

  document.addEventListener("input", function (ev) {
    var form = ev.target.form;
    if (form && form.closest("dialog")) form.dataset.dirty = "1";
  });

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      ev.preventDefault();
      return;
    }
    if (form.dataset.enviando === "1") { ev.preventDefault(); return; }
    form.dataset.enviando = "1";
    delete form.dataset.dirty;
    if (form.hasAttribute("data-busy")) {
      var botao = ev.submitter || form.querySelector("button[type=submit], button:not([type])");
      if (botao) {
        botao.dataset.rotulo = botao.innerHTML;
        botao.textContent = form.dataset.busy || "Salvando…";
        botao.setAttribute("aria-disabled", "true");
      }
    }
  });

  // form[data-somente-leitura]: usuário sem permissão vê os valores, sem editar
  // (form.elements inclui os campos ligados por form="id" fora do <form>)
  document.querySelectorAll("form[data-somente-leitura]").forEach(function (f) {
    Array.from(f.elements).forEach(function (campo) { campo.disabled = true; });
  });

  // voltar do cache do navegador: libera o formulário de novo
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-enviando]").forEach(function (f) {
      delete f.dataset.enviando;
      f.querySelectorAll("[data-rotulo]").forEach(function (b) {
        b.innerHTML = b.dataset.rotulo; b.removeAttribute("aria-disabled");
      });
    });
  });

  // mensagens do servidor: botão de dispensar
  document.addEventListener("click", function (ev) {
    var x = ev.target.closest("[data-dismiss]");
    if (x) x.closest(".notice").remove();
  });

  // ícones da biblioteca existente (Lucide), 20px e traço consistente
  function icones() {
    if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.65 } });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", icones);
  else icones();
})();

/* Mostrar/ocultar senha */
document.addEventListener("click", function (ev) {
  var b = ev.target.closest("[data-toggle-senha]");
  if (!b) return;
  var campo = document.getElementById(b.dataset.toggleSenha);
  var mostrar = campo.type === "password";
  campo.type = mostrar ? "text" : "password";
  b.textContent = mostrar ? "Ocultar" : "Mostrar";
  b.setAttribute("aria-pressed", mostrar ? "true" : "false");
});
