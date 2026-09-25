/* Gráficos do Bangalô no padrão Clareza (design-tokens.json → chart).
   Realizado em laranja contínuo; comparação/meta em cinza tracejado (não depende
   só de cor); grade recessiva; sem gradientes; lacunas não viram zero. */
(function () {
  "use strict";

  const COR = {
    realizado: "#CD5C27",
    anterior: "#84909F",
    meta: "#596575",
    grade: "#E8ECF1",
    texto: "#596575",
    tinta: "#202329",
    borda: "#DEE3EA",
    entrada: "#237044",
    saida: "#B3261E",
  };
  const TRACO = 2.5;
  const MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

  const brl = (v) =>
    v == null ? "—" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

  function brlCompacto(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " mi";
    if (a >= 1e3) return Math.round(v / 1e3) + " mil";
    return "R$ " + Math.round(v);
  }

  function defaults() {
    if (!window.Chart) return;
    Chart.defaults.font.family = "'IBM Plex Sans', Arial, sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.color = COR.texto;
  }

  const reduzido = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const eixos = {
    x: { grid: { display: false }, border: { display: false }, ticks: { color: COR.texto, maxTicksLimit: 8 } },
    y: { grid: { color: COR.grade }, border: { display: false }, beginAtZero: true,
         ticks: { color: COR.texto, maxTicksLimit: 4, callback: (v) => brlCompacto(v) } },
  };

  const tooltip = {
    backgroundColor: "#FFFFFF", titleColor: COR.tinta, bodyColor: COR.tinta,
    borderColor: COR.borda, borderWidth: 1, padding: 12, cornerRadius: 8,
    boxPadding: 4, usePointStyle: true,
    titleFont: { weight: "600" },
    callbacks: { label: (c) => " " + c.dataset.label + ": " + brl(c.raw) },
  };

  function opcoes(extra) {
    return Object.assign({
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip },
      scales: eixos,
      animation: reduzido ? false : { duration: 160, easing: "easeOutQuad" },
    }, extra || {});
  }

  function linha(label, data, cor, tracejado) {
    return {
      type: "line", label, data, borderColor: cor, backgroundColor: cor,
      borderWidth: TRACO, borderDash: tracejado ? [5, 5] : [], tension: 0,
      pointRadius: tracejado ? 0 : 3, pointHoverRadius: 5, spanGaps: false, fill: false,
    };
  }

  function barra(label, data, cor) {
    return { type: "bar", label, data, backgroundColor: cor, borderRadius: 4, maxBarThickness: 28 };
  }

  // Início: faturamento acumulado do mês × meta acumulada
  function faturamentoMeta(canvas, dados) {
    return new Chart(canvas, {
      data: {
        labels: dados.map((d) => d.dia),
        datasets: [
          linha("Faturamento", dados.map((d) => d.realizado), COR.realizado, false),
          linha("Meta", dados.map((d) => d.meta), COR.meta, true),
        ],
      },
      options: opcoes(),
    });
  }

  // Metas: realizado (barras) × meta (linha tracejada) por mês
  function metaVsRealizado(canvas, _legendaEl, dados) {
    return new Chart(canvas, {
      data: {
        labels: MESES,
        datasets: [
          Object.assign(barra("Realizado", dados.map((d) => d.realizado), COR.realizado), { order: 2 }),
          Object.assign(linha("Meta", dados.map((d) => d.meta), COR.meta, true), { order: 1 }),
        ],
      },
      options: opcoes(),
    });
  }

  // Fluxo mensal: entradas e saídas (barras) + resultado (linha)
  function fluxoMensal(canvas, _legendaEl, dados) {
    return new Chart(canvas, {
      data: {
        labels: MESES,
        datasets: [
          Object.assign(barra("Entradas", dados.map((d) => d.entradas), COR.entrada), { order: 2 }),
          Object.assign(barra("Saídas", dados.map((d) => d.saidas), COR.anterior), { order: 2 }),
          Object.assign(linha("Resultado", dados.map((d) => d.resultado), COR.realizado, false), { order: 1 }),
        ],
      },
      options: opcoes(),
    });
  }

  // Despesas por grupo: barras horizontais (comparação direta, sem donut)
  function donutDespesas(canvas, _legendaEl, dados) {
    if (!dados || !dados.length) return;
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels: dados.map((d) => d.grupo),
        datasets: [{ label: "Despesa", data: dados.map((d) => d.total), backgroundColor: COR.realizado,
                     borderRadius: 4, maxBarThickness: 22 }],
      },
      options: opcoes({
        indexAxis: "y",
        interaction: { mode: "nearest", intersect: true, axis: "y" },
        scales: {
          x: { grid: { color: COR.grade }, border: { display: false }, beginAtZero: true,
               ticks: { color: COR.texto, maxTicksLimit: 4, callback: (v) => brlCompacto(v) } },
          y: { grid: { display: false }, border: { display: false }, ticks: { color: COR.tinta } },
        },
      }),
    });
  }

  window.BangaloCharts = { defaults, faturamentoMeta, metaVsRealizado, fluxoMensal, donutDespesas, brl, brlCompacto, COR };
})();
