/* Gráficos do Bangalô — estilo moderno e consistente sobre Chart.js.
   Método dataviz: forma certa, marcas finas arredondadas, grade recessiva,
   legenda própria, tooltip claro, eixo em R$ compacto. */
(function () {
  "use strict";

  const MARCA = "#CD5C27";      // laranja da marca (destaque)
  const MARCA_CLARO = "#E08E60";
  const NEUTRO = "#CBB9A8";     // referência neutra (meta)
  const ENTRADA = "#2E7D32";    // verde (fluxo de entrada)
  const SAIDA = "#B3261E";      // vermelho (fluxo de saída)
  const TINTA = "#2B2B2B";
  const MUTED = "#9A8E80";
  const GRADE = "rgba(43,43,43,0.06)";

  const MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"];

  const brl = (v) =>
    v == null ? "—" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

  function brlCompacto(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e6) return "R$ " + (v / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " mi";
    if (a >= 1e3) return "R$ " + Math.round(v / 1e3) + " mil";
    return "R$ " + Math.round(v);
  }

  function defaults() {
    if (!window.Chart) return;
    Chart.defaults.font.family = "Inter, sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.color = MUTED;
  }

  // gradiente vertical suave para as barras/áreas
  function grad(ctx, area, cor, alphaTopo, alphaBase) {
    if (!area) return cor;
    const g = ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, hexA(cor, alphaTopo));
    g.addColorStop(1, hexA(cor, alphaBase));
    return g;
  }
  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  const eixos = {
    x: { grid: { display: false }, border: { display: false },
         ticks: { color: MUTED, font: { weight: "600" } } },
    y: { grid: { color: GRADE }, border: { display: false }, beginAtZero: true,
         ticks: { color: MUTED, maxTicksLimit: 5, callback: (v) => brlCompacto(v) } },
  };

  const tooltip = {
    backgroundColor: "#2B2B2B", titleColor: "#fff", bodyColor: "#F3E9DF",
    padding: 12, cornerRadius: 10, displayColors: true, boxPadding: 4,
    titleFont: { family: "Poppins, sans-serif", weight: "600", size: 12 },
    bodyFont: { size: 12 }, borderColor: "rgba(255,255,255,0.06)", borderWidth: 1,
    callbacks: { label: (c) => "  " + c.dataset.label + ": " + brl(c.raw) },
  };

  const barraBase = {
    type: "bar", borderRadius: 6, borderSkipped: false, borderWidth: 0,
    barPercentage: 0.66, categoryPercentage: 0.62, maxBarThickness: 34,
  };
  const linhaBase = {
    type: "line", tension: 0.38, borderWidth: 2.5, pointRadius: 0,
    pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: "#fff",
    fill: false, order: 0,
  };

  function opcoes(extra) {
    return Object.assign({
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip },
      scales: eixos,
      animation: { duration: 700, easing: "easeOutQuart" },
    }, extra || {});
  }

  // legenda própria (dots/linhas) num container ao lado do título
  function legenda(el, itens) {
    if (!el) return;
    el.innerHTML = itens.map((i) => `
      <span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#6B6B6B">
        <span style="width:${i.linha ? 14 : 9}px;height:${i.linha ? 3 : 9}px;border-radius:${i.linha ? 2 : 9}px;
                     background:${i.cor};display:inline-block;${i.tracejado ? "background:repeating-linear-gradient(90deg," + i.cor + " 0 4px,transparent 4px 7px)" : ""}"></span>
        ${i.rotulo}
      </span>`).join("");
  }

  // ---- Realizado × Meta: barras (realizado) + linha de referência (meta) ----
  function metaVsRealizado(canvas, legendaEl, dados) {
    const meta = dados.map((d) => d.meta);
    const real = dados.map((d) => d.realizado);
    legenda(legendaEl, [
      { cor: MARCA, rotulo: "Realizado" },
      { cor: NEUTRO, rotulo: "Meta", linha: true, tracejado: true },
    ]);
    return new Chart(canvas, {
      data: {
        labels: MESES,
        datasets: [
          Object.assign({
            label: "Realizado", data: real, order: 1,
            backgroundColor: (c) => grad(c.chart.ctx, c.chart.chartArea, MARCA, 0.95, 0.6),
            hoverBackgroundColor: MARCA,
          }, barraBase),
          Object.assign({}, linhaBase, {
            label: "Meta", data: meta, borderColor: NEUTRO, borderDash: [5, 4],
            pointHoverBackgroundColor: NEUTRO,
          }),
        ],
      },
      options: opcoes(),
    });
  }

  // ---- Fluxo mensal: entradas/saídas (barras) + resultado (linha) ----
  function fluxoMensal(canvas, legendaEl, dados) {
    legenda(legendaEl, [
      { cor: ENTRADA, rotulo: "Entradas" },
      { cor: SAIDA, rotulo: "Saídas" },
      { cor: MARCA, rotulo: "Resultado", linha: true },
    ]);
    return new Chart(canvas, {
      data: {
        labels: MESES,
        datasets: [
          Object.assign({ label: "Entradas", data: dados.map((d) => d.entradas), order: 2,
            backgroundColor: (c) => grad(c.chart.ctx, c.chart.chartArea, ENTRADA, 0.9, 0.55),
            hoverBackgroundColor: ENTRADA }, barraBase),
          Object.assign({ label: "Saídas", data: dados.map((d) => d.saidas), order: 2,
            backgroundColor: (c) => grad(c.chart.ctx, c.chart.chartArea, SAIDA, 0.9, 0.55),
            hoverBackgroundColor: SAIDA }, barraBase),
          Object.assign({}, linhaBase, { label: "Resultado", data: dados.map((d) => d.resultado),
            borderColor: MARCA, pointHoverBackgroundColor: MARCA }),
        ],
      },
      options: opcoes(),
    });
  }

  window.BangaloCharts = { defaults, metaVsRealizado, fluxoMensal, brl, brlCompacto };
})();
