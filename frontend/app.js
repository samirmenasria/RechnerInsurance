// Prämienrechner OKP – Frontend (ohne Build-Schritt, ES-Module)
// Rechnet für eine Person oder einen ganzen Haushalt (Paare, Familien).

const SPRACHE = (new URLSearchParams(location.search).get("lang") || "de").slice(0, 2);
const SEITE = 25;        // Zeilen vor «alle anzeigen»
const MAX_PERSONEN = 10;

const state = {
  texte: {},
  stamm: null,
  meta: null,
  gemeinde: null,        // gewählte Gemeinde (Objekt aus /api/orte)
  personen: [],          // [{id, el, akl}]
  naechsteId: 1,
  modus: "gemeinsam",    // gemeinsam | kombination
  resultat: null,        // Antwort von /api/haushalt
  kombiWahl: [],         // je Person gewählter Tarif in der Kombination
  alleZeigen: false,
  anfrage: null,         // {modus, tarifIds, zeilen, monat}
};

// ------------------------------------------------------------------ Hilfsfunktionen
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, attrs = {}, ...kinder) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kind of kinder.flat()) {
    if (kind === null || kind === undefined || kind === false) continue;
    n.append(kind instanceof Node ? kind : document.createTextNode(String(kind)));
  }
  return n;
};

function t(key, vars = {}) {
  const s = state.texte[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] ?? `{${k}}`));
}

const fmt = new Intl.NumberFormat("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmt0 = new Intl.NumberFormat("de-CH", { maximumFractionDigits: 0 });
const chf = (v) => (v === null || v === undefined ? "–" : `CHF ${fmt.format(v)}`);
const diff = (v) => (v === null || v === undefined || v === 0 ? "–" : `${v > 0 ? "+" : "−"}${fmt.format(Math.abs(v))}`);
const rund = (v) => Math.round(v * 100) / 100;

async function api(pfad, params, optionen = {}) {
  const url = new URL(pfad, location.origin);
  if (params) for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
  const res = await fetch(url, optionen);
  const daten = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(daten.detail) ? daten.detail.map((d) => d.msg).join(", ") : daten.detail;
    throw new Error(detail || res.statusText);
  }
  return daten;
}
const post = (pfad, body) => api(pfad, null, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function debounce(fn, ms) {
  let h;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

function uebersetzen(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((n) => { n.textContent = t(n.dataset.i18n, { jahr: state.meta?.praemienjahr ?? "" }); });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((n) => { n.placeholder = t(n.dataset.i18nPlaceholder); });
}

// ------------------------------------------------------------------ Initialisierung
async function init() {
  state.texte = await fetch(`/static/i18n/${SPRACHE}.json`).then((r) => r.json())
    .catch(() => fetch("/static/i18n/de.json").then((r) => r.json()));
  [state.meta, state.stamm] = await Promise.all([api("/api/meta"), api("/api/stammdaten")]);
  document.title = t("app.titel", { jahr: state.meta.praemienjahr });
  uebersetzen();

  $("#footer-quelle").textContent = t("footer.quelle", {
    jahr: state.meta.praemienjahr,
    erhebung: state.meta.erhebungsjahr ?? state.meta.praemienjahr - 1,
    stand: new Date(state.meta.stand).toLocaleDateString("de-CH"),
  });
  $("#footer-einzugsgebiete").hidden = state.stamm.einzugsgebiete_vorhanden;

  const modell = $("#modell");
  modell.append(el("option", { value: "alle" }, t("form.modell.alle")));
  state.stamm.modelle.forEach((m) => modell.append(el("option", { value: m.code }, m.name_de)));

  const akt = $("#aktueller-versicherer");
  akt.append(el("option", { value: "" }, t("form.aktuell.keiner")));
  state.stamm.versicherer.forEach((v) => akt.append(el("option", { value: v.bag_nr }, v.name_kurz)));

  bindeOrtssuche();
  bindeFormular();
  bindeErgebnis();
  bindeKosten();
  bindeDialoge();
  if (!(await zustandAusUrl())) personHinzufuegen();
}

// ------------------------------------------------------------------ Ortssuche
function bindeOrtssuche() {
  const input = $("#ort");
  const liste = $("#ort-liste");
  let treffer = [];
  let aktiv = -1;

  const schliessen = () => { liste.hidden = true; input.setAttribute("aria-expanded", "false"); aktiv = -1; };

  const zeigen = (daten) => {
    treffer = daten.gemeinden;
    liste.replaceChildren();
    const meldung = $("#ort-meldung");
    meldung.hidden = true;
    if (!treffer.length) {
      meldung.textContent = t("form.ort.keine");
      meldung.hidden = false;
      schliessen();
      return;
    }
    if (treffer.length === 1 && /^\d{4}$/.test(daten.query)) {
      waehleGemeinde(treffer[0]);
      return;
    }
    if (/^\d{4}$/.test(daten.query) && treffer.length > 1) {
      meldung.textContent = daten.mehrere_regionen ? t("form.ort.mehrere_regionen") : t("form.ort.mehrere");
      meldung.hidden = false;
    }
    treffer.forEach((g, i) => {
      const orte = g.orte.slice(0, 3).join(", ") + (g.orte.length > 3 ? " …" : "");
      liste.append(el("li", {
        role: "option", id: `ort-opt-${i}`,
        onmousedown: (e) => { e.preventDefault(); waehleGemeinde(g); },
      },
      el("span", {}, el("strong", {}, g.gemeinde), ` (${g.kanton})`),
      el("span", { class: "region" }, `Region ${g.region_nr}`),
      el("small", {}, orte)));
    });
    liste.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };

  const suchen = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { schliessen(); return; }
    try { zeigen(await api("/api/orte", { q })); } catch (e) { console.error(e); }
  }, 200);

  input.addEventListener("input", () => { state.gemeinde = null; suchen(); });
  input.addEventListener("keydown", (e) => {
    if (liste.hidden) return;
    const items = [...liste.children];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      aktiv = (aktiv + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach((n, i) => n.classList.toggle("aktiv", i === aktiv));
      input.setAttribute("aria-activedescendant", items[aktiv].id);
    } else if (e.key === "Enter" && aktiv >= 0) {
      e.preventDefault();
      waehleGemeinde(treffer[aktiv]);
    } else if (e.key === "Escape") {
      schliessen();
    }
  });
  input.addEventListener("blur", () => setTimeout(schliessen, 150));
  $("#ort-aendern").addEventListener("click", () => {
    state.gemeinde = null;
    $("#ort-gewaehlt").hidden = true;
    $("#ort-suche").hidden = false;
    input.value = "";
    input.focus();
  });
}

function waehleGemeinde(g, rechnen = true) {
  state.gemeinde = g;
  $("#ort-liste").hidden = true;
  $("#ort-meldung").hidden = true;
  $("#ort").value = `${g.plz[0]} ${g.gemeinde}`;
  $("#ort-suche").hidden = true;
  $("#ort-gewaehlt-text").textContent = t("form.ort.gewaehlt", { gemeinde: g.gemeinde, kanton: g.kanton, region: g.region_nr });
  $("#ort-gewaehlt").hidden = false;
  if (rechnen) berechnenWennBereit();
}

// ------------------------------------------------------------------ Personen
function personHinzufuegen(daten = {}) {
  if (state.personen.length >= MAX_PERSONEN) return null;
  const id = state.naechsteId++;
  const node = $("#person-vorlage").content.firstElementChild.cloneNode(true);
  uebersetzen(node);
  // eindeutige IDs für Labels und Radio-Gruppen
  const name = $(".p-name", node), jahr = $(".p-jahr", node), fr = $(".p-franchise", node);
  name.id = `p${id}-name`; jahr.id = `p${id}-jahr`; fr.id = `p${id}-franchise`;
  $("[data-feld=name]", node).htmlFor = name.id;
  $("[data-feld=jahr]", node).htmlFor = jahr.id;
  $("[data-feld=franchise]", node).htmlFor = fr.id;
  $$(".p-unfall", node).forEach((r) => { r.name = `p${id}-unfall`; });

  const person = { id, el: node, akl: null };
  state.personen.push(person);
  $("#personen").append(node);

  if (daten.name) name.value = daten.name;
  if (daten.unfall === false) $(".p-unfall[value=false]", node).checked = true;
  jahr.addEventListener("input", debounce(() => aktualisiereAltersklasse(person), 250));
  name.addEventListener("change", () => { if (state.resultat) berechnen(); });
  fr.addEventListener("change", berechnenWennBereit);
  $$(".p-unfall", node).forEach((r) => r.addEventListener("change", berechnenWennBereit));
  $(".person-entfernen", node).addEventListener("click", () => personEntfernen(person));

  nummeriere();
  if (daten.geburtsjahr) {
    jahr.value = daten.geburtsjahr;
    return aktualisiereAltersklasse(person, daten.franchise, false).then(() => person);
  }
  return Promise.resolve(person);
}

function personEntfernen(person) {
  state.personen = state.personen.filter((p) => p !== person);
  person.el.remove();
  nummeriere();
  berechnenWennBereit();
}

function nummeriere() {
  const mehrere = state.personen.length > 1;
  state.personen.forEach((p, i) => {
    $(".person-titel", p.el).textContent = t("form.person.titel", { n: i + 1 });
    $(".person-entfernen", p.el).hidden = !mehrere;
  });
  $("#person-plus").hidden = state.personen.length >= MAX_PERSONEN;
  $("#aktuelle-praemie-label").textContent = mehrere ? t("form.aktuell.praemie.haushalt") : t("form.aktuell.praemie");
  aktualisiereKinderrabattFeld();
}

function aktualisiereKinderrabattFeld() {
  const kinder = state.personen.filter((p) => p.akl?.code === "AKL-KIN").length;
  $("#kinderrabatt-feld").hidden = kinder < 2;
}

async function aktualisiereAltersklasse(person, gewuenschteFranchise, rechnen = true) {
  const jahrInput = $(".p-jahr", person.el);
  const info = $(".p-info", person.el);
  const fr = $(".p-franchise", person.el);
  const jahr = parseInt(jahrInput.value, 10);
  if (!jahr || jahr < 1900 || jahr > state.meta.praemienjahr) {
    person.akl = null;
    info.textContent = jahrInput.value.length >= 4 ? t("form.geburtsjahr.fehler") : "";
    fr.disabled = true;
    aktualisiereKinderrabattFeld();
    return;
  }
  try {
    const akl = await api("/api/altersklasse", { geburtsjahr: jahr });
    const alt = person.akl?.code;
    person.akl = akl;
    info.textContent = t("form.geburtsjahr.info", { alter: akl.alter, jahr: akl.praemienjahr, klasse: akl.name });
    if (alt !== akl.code || gewuenschteFranchise) fuelleFranchisen(fr, akl.code, gewuenschteFranchise);
    aktualisiereKinderrabattFeld();
    if (rechnen) berechnenWennBereit();
  } catch (e) {
    person.akl = null;
    info.textContent = e.message;
  }
}

function fuelleFranchisen(sel, code, gewuenscht) {
  const klasse = state.stamm.altersklassen.find((a) => a.code === code);
  const bisher = gewuenscht || sel.value;
  sel.replaceChildren(...klasse.franchisen.map((f) =>
    el("option", { value: f.code }, `CHF ${fmt0.format(f.betrag)}${f.ist_ordentlich ? ` (${t("form.franchise.ordentlich")})` : ""}`)));
  const ordentlich = klasse.franchisen.find((f) => f.ist_ordentlich)?.code;
  sel.value = klasse.franchisen.some((f) => f.code === bisher) ? bisher : ordentlich;
  sel.disabled = false;
}

function personDaten(p, i) {
  return {
    name: $(".p-name", p.el).value.trim() || t("form.person.titel", { n: i + 1 }),
    geburtsjahr: parseInt($(".p-jahr", p.el).value, 10),
    mit_unfall: $(".p-unfall:checked", p.el).value === "true",
    franchise: $(".p-franchise", p.el).value,
  };
}

// ------------------------------------------------------------------ Formular / Berechnung
function bindeFormular() {
  $("#person-plus").addEventListener("click", async () => {
    const p = await personHinzufuegen();
    $(".p-name", p.el).focus();
  });
  ["#modell", "#kinderrabatt", "#aktueller-versicherer"].forEach((s) => $(s).addEventListener("change", berechnenWennBereit));
  $("#aktuelle-praemie").addEventListener("input", debounce(berechnenWennBereit, 400));
  $("#rechner").addEventListener("submit", (e) => { e.preventDefault(); berechnen(); });
}

function eingaben() {
  return {
    bfs_nr: state.gemeinde?.bfs_nr,
    personen: state.personen.map(personDaten),
    modell: $("#modell").value === "alle" ? null : $("#modell").value,
    kinderrabatt: $("#kinderrabatt").checked,
    aktueller_versicherer: $("#aktueller-versicherer").value ? parseInt($("#aktueller-versicherer").value, 10) : null,
    aktuelle_praemie: $("#aktuelle-praemie").value ? parseFloat($("#aktuelle-praemie").value) : null,
  };
}

const bereit = () => state.gemeinde && state.personen.length
  && state.personen.every((p) => p.akl && $(".p-franchise", p.el).value);

function berechnenWennBereit() { if (bereit()) berechnen(); }

async function berechnen() {
  if (!bereit()) {
    if (!state.gemeinde) { $("#ort").focus(); return; }
    const offen = state.personen.find((p) => !p.akl);
    if (offen) $(".p-jahr", offen.el).focus();
    return;
  }
  const p = eingaben();
  try {
    state.resultat = await post("/api/haushalt", p);
    state.kombiWahl = state.resultat.kombination
      ? state.resultat.kombination.personen.map((x) => x.tarif_id) : [];
    if (state.personen.length === 1) state.modus = "gemeinsam";
    state.alleZeigen = false;
    zeichneResultat();
    fuellePersonenFuerKosten();
    ladeKosten();
    zustandInUrl(p);
  } catch (e) {
    $("#resultat").hidden = true;
    $("#resultat-leer").hidden = false;
    $("#resultat-leer").replaceChildren(el("p", { class: "meldung fehler" }, t("allgemein.fehler", { fehler: e.message })));
  }
}

// ------------------------------------------------------------------ Resultate
function bindeErgebnis() {
  $$("#modus button").forEach((b) => b.addEventListener("click", () => {
    state.modus = b.dataset.modus;
    zeichneResultat();
    zustandInUrl(eingaben());
  }));
  $("#filter").addEventListener("input", zeichneTabelle);
  $("#mehr").addEventListener("click", () => { state.alleZeigen = !state.alleZeigen; zeichneTabelle(); });
  $("#kombi-anfragen").addEventListener("click", () => {
    const zeilen = kombiZeilen();
    oeffneAnfrage({ modus: "kombination", zeilen, monat: rund(zeilen.reduce((s, z) => s + z.monat, 0)) });
  });
}

function personenText(personen) {
  return personen.map((p) => `${p.name} (${p.geburtsjahr}, CHF ${fmt0.format(p.franchise_betrag)})`).join(", ");
}

function zeichneResultat() {
  const r = state.resultat;
  const g = r.kontext.gemeinde;
  const haushalt = r.personen.length > 1;
  const gem = r.gemeinsam;
  const kb = r.kombination;
  $("#resultat-leer").hidden = true;
  $("#resultat").hidden = false;

  if (haushalt) {
    $("#resultat-titel").textContent = t("resultat.titel.haushalt", { anzahl: r.personen.length });
    $("#resultat-kontext").textContent = t("resultat.kontext.haushalt", {
      gemeinde: g.gemeinde, kanton: g.kanton, region: g.region_nr, personen: personenText(r.personen),
    });
  } else {
    const p = r.personen[0];
    $("#resultat-titel").textContent = t("resultat.titel", { anzahl: gem.anzahl });
    $("#resultat-kontext").textContent = t("resultat.kontext", {
      gemeinde: g.gemeinde, kanton: g.kanton, region: g.region_nr, klasse: p.altersklasse_name,
      franchise: `CHF ${fmt0.format(p.franchise_betrag)}`, unfall: p.mit_unfall ? t("form.unfall.ja") : t("form.unfall.nein"),
    });
  }

  // Umschalter der Varianten
  $("#modus").hidden = !haushalt;
  $$("#modus button").forEach((b) => {
    const an = b.dataset.modus === state.modus;
    b.classList.toggle("an", an);
    b.setAttribute("aria-checked", String(an));
  });
  $("#modus-hilfe").hidden = !haushalt;
  $("#modus-hilfe").textContent = state.modus === "gemeinsam" ? t("modus.hilfe.gemeinsam") : t("modus.hilfe.kombination");
  $("#ansicht-gemeinsam").hidden = haushalt && state.modus !== "gemeinsam";
  $("#ansicht-kombination").hidden = !haushalt || state.modus !== "kombination";

  // Kennzahlen
  const kz = $("#kennzahlen");
  kz.replaceChildren();
  if (gem.angebote.length) {
    const a = gem.angebote[0];
    kz.append(el("div", { class: `kennzahl ${state.modus === "gemeinsam" ? "haupt" : ""}` },
      el("span", {}, haushalt ? t("resultat.guenstigste_gemeinsam") : t("resultat.guenstigste")),
      el("strong", {}, chf(a.monat), el("small", {}, ` ${t("pro_monat")}`)),
      el("small", {}, `${a.versicherer} · ${a.tarifbezeichnung}`)));
  }
  if (haushalt && kb) {
    kz.append(el("div", { class: `kennzahl ${state.modus === "kombination" ? "haupt" : ""}` },
      el("span", {}, t("resultat.guenstigste_kombination")),
      el("strong", {}, chf(kb.monat), el("small", {}, ` ${t("pro_monat")}`)),
      el("small", {}, t("kombination.versicherer", { anzahl: kb.anzahl_versicherer }))));
  }
  if (r.aktuell) {
    const a = r.aktuell;
    kz.append(el("div", { class: "kennzahl" },
      el("span", {}, t("resultat.aktuell")),
      el("strong", {}, chf(a.monat), el("small", {}, ` ${t("pro_monat")}`)),
      el("small", {}, a.versicherer ? `${a.versicherer} · ${a.tarifbezeichnung}` : "")));
    const bester = state.modus === "kombination" && kb ? kb.monat : gem.angebote[0]?.monat;
    if (bester !== undefined) {
      kz.append(el("div", { class: "kennzahl gut" },
        el("span", {}, t("resultat.ersparnis")), el("strong", {}, chf(Math.max(0, (a.monat - bester) * 12)))));
    }
  } else if (r.hinweis_aktuell) {
    kz.append(el("p", { class: "hilfe" }, r.hinweis_aktuell));
  }

  $("#spalte-praemie").textContent = haushalt ? t("spalte.praemie.haushalt") : t("spalte.praemie");
  document.querySelectorAll(".spalte-aktuell").forEach((n) => { n.hidden = !r.aktuell; });
  zeichneTabelle();
  zeichneKombination();
}

function zeichneTabelle() {
  const r = state.resultat;
  if (!r) return;
  const haushalt = r.personen.length > 1;
  const q = $("#filter").value.trim().toLowerCase();
  let zeilen = r.gemeinsam.angebote.filter((a) => !q || `${a.versicherer} ${a.tarifbezeichnung} ${a.modell}`.toLowerCase().includes(q));
  const total = zeilen.length;
  if (!state.alleZeigen && !q) zeilen = zeilen.slice(0, SEITE);

  $("#tabelle tbody").replaceChildren(...zeilen.map((a) => el("tr", { class: a.ist_aktuell ? "ist-aktuell" : null },
    el("td", { "data-label": t("spalte.rang") }, a.rang),
    el("td", { "data-label": t("spalte.versicherer") },
      el("button", { type: "button", class: "link", onclick: () => zeigeVersicherer(a.bag_nr) }, a.versicherer),
      a.ist_aktuell ? el("span", { class: "badge" }, t("zeile.aktuell")) : null),
    el("td", { "data-label": t("spalte.modell") },
      el("span", { class: "modell" }, a.modell), " ", a.tarifbezeichnung,
      a.rabatt ? el("span", { class: "badge rabatt" }, t("zeile.rabatt.kurz")) : null,
      haushalt ? el("small", { class: "unter personen-aufteilung" },
        a.personen.map((p) => `${p.name} ${fmt.format(p.monat)}`).join(" · ")) : null),
    el("td", { class: "zahl", "data-label": haushalt ? t("spalte.praemie.haushalt") : t("spalte.praemie") },
      el("strong", {}, chf(a.monat)), el("small", { class: "unter jahr" }, `${chf(a.jahr)} ${t("pro_jahr")}`),
      a.diff_guenstigste_monat ? el("small", { class: "unter" }, t("zeile.diff_guenstigste", { diff: diff(a.diff_guenstigste_monat) })) : null),
    el("td", { class: `zahl spalte-aktuell ${a.diff_aktuell_monat < 0 ? "gut" : a.diff_aktuell_monat > 0 ? "schlecht" : ""}`,
      "data-label": t("spalte.diff_aktuell"), hidden: !r.aktuell }, diff(a.diff_aktuell_monat)),
    el("td", { class: "aktion" },
      el("button", { type: "button", class: "primaer klein", title: t("anfrage.titel"),
        onclick: () => oeffneAnfrage({ modus: "gemeinsam", zeilen: a.personen, monat: a.monat, angebot: a }) },
      t("zeile.anfragen"))),
  )));
  $("#keine-angebote").hidden = total > 0;
  const mehr = $("#mehr");
  mehr.hidden = !!q || total <= SEITE;
  mehr.textContent = state.alleZeigen ? t("resultat.weniger") : t("resultat.alle", { anzahl: total });
}

function kombiZeilen() {
  const r = state.resultat;
  return r.personen.map((p, i) => r.alternativen[i].find((a) => a.tarif_id === state.kombiWahl[i])
    || r.kombination.personen[i]);
}

function zeichneKombination() {
  const r = state.resultat;
  const body = $("#kombi-tabelle tbody");
  const fuss = $("#kombi-tabelle tfoot");
  if (!r.kombination) {
    body.replaceChildren();
    fuss.replaceChildren();
    $("#kombi-keine").hidden = false;
    $("#kombi-anfragen").hidden = true;
    return;
  }
  $("#kombi-keine").hidden = true;
  $("#kombi-anfragen").hidden = false;
  const zeilen = kombiZeilen();
  body.replaceChildren(...r.personen.map((p, i) => {
    const a = zeilen[i];
    const auswahl = el("select", {
      class: "kombi-wahl", "aria-label": t("kombination.alternative", { name: p.name }),
      onchange: (e) => { state.kombiWahl[i] = parseInt(e.target.value, 10); zeichneKombination(); },
    }, r.alternativen[i].map((x) => el("option", { value: x.tarif_id, selected: x.tarif_id === a.tarif_id },
      `${x.versicherer} – ${x.tarifbezeichnung}: ${chf(x.monat)}`)));
    return el("tr", {},
      el("td", { "data-label": t("kombination.person") }, el("strong", {}, p.name),
        el("small", { class: "unter" }, `${p.geburtsjahr} · CHF ${fmt0.format(p.franchise_betrag)} · ${p.mit_unfall ? t("form.unfall.ja") : t("form.unfall.nein")}`)),
      el("td", { "data-label": t("spalte.versicherer") },
        el("button", { type: "button", class: "link", onclick: () => zeigeVersicherer(a.bag_nr) }, a.versicherer),
        el("div", {}, auswahl)),
      el("td", { "data-label": t("spalte.modell") }, el("span", { class: "modell" }, a.modell), " ", a.tarifbezeichnung,
        a.rabatt ? el("span", { class: "badge rabatt" }, t("zeile.rabatt.kurz")) : null),
      el("td", { class: "zahl stark", "data-label": t("kombination.praemie") }, chf(a.monat)));
  }));
  const monat = rund(zeilen.reduce((s, z) => s + z.monat, 0));
  const gem0 = r.gemeinsam.angebote[0]?.monat;
  fuss.replaceChildren(el("tr", { class: "summe" },
    el("td", { colspan: 3 }, t("kombination.total"),
      gem0 !== undefined ? el("small", { class: "unter" }, t("kombination.vergleich", { diff: diff(rund(monat - gem0)) })) : null),
    el("td", { class: "zahl" }, el("strong", {}, chf(monat)), el("small", { class: "unter jahr" }, `${chf(rund(monat * 12))} ${t("pro_jahr")}`))));
}

// ------------------------------------------------------------------ Gesamtkosten (pro Person)
function bindeKosten() {
  const betrag = $("#kosten-betrag");
  const slider = $("#kosten-slider");
  const neu = debounce(ladeKosten, 250);
  betrag.addEventListener("input", () => { slider.value = betrag.value; neu(); });
  slider.addEventListener("input", () => { betrag.value = slider.value; neu(); });
  $("#kosten-tarif").addEventListener("change", ladeKosten);
  $("#kosten-person").addEventListener("change", ladeKosten);
}

function fuellePersonenFuerKosten() {
  const r = state.resultat;
  const sel = $("#kosten-person");
  const bisher = sel.value;
  sel.replaceChildren(...r.personen.map((p, i) => el("option", { value: i }, `${p.name} (${p.geburtsjahr})`)));
  if ([...sel.options].some((o) => o.value === bisher)) sel.value = bisher;
  $("#kosten-person-feld").hidden = r.personen.length < 2;

  const tarif = $("#kosten-tarif");
  const bisherT = tarif.value;
  tarif.replaceChildren(el("option", { value: "" }, t("kosten.angebot.guenstigstes")),
    ...r.gemeinsam.angebote.slice(0, 50).map((a) => el("option", { value: a.tarif_id }, `${a.versicherer} – ${a.tarifbezeichnung}`)));
  if ([...tarif.options].some((o) => o.value === bisherT)) tarif.value = bisherT;
}

async function ladeKosten() {
  const r = state.resultat;
  if (!r) return;
  const p = r.personen[parseInt($("#kosten-person").value || "0", 10)] || r.personen[0];
  const kosten = Math.max(0, parseFloat($("#kosten-betrag").value) || 0);
  try {
    const k = await api("/api/gesamtkosten", {
      bfs: r.kontext.gemeinde.bfs_nr, geburtsjahr: p.geburtsjahr, unfall: p.mit_unfall,
      modell: r.kontext.modell, kinderrabatt: p.geschwisterrabatt, kosten, tarif_id: $("#kosten-tarif").value || null,
    });
    $("#kosten-text").textContent = t("kosten.text", { max: fmt0.format(k.max_selbstbehalt) });
    $("#kosten-tabelle tbody").replaceChildren(...k.zeilen.map((z) => el("tr", { class: z.optimal ? "optimal" : null },
      el("td", { "data-label": t("kosten.franchise") }, `CHF ${fmt0.format(z.franchise_betrag)}`,
        z.optimal ? el("span", { class: "badge gut" }, t("kosten.optimal")) : null),
      z.angebot
        ? [el("td", { "data-label": t("kosten.angebot") }, `${z.angebot.versicherer} – ${z.angebot.tarifbezeichnung}`),
          el("td", { class: "zahl", "data-label": t("kosten.praemie") }, chf(z.jahrespraemie)),
          el("td", { class: "zahl", "data-label": t("kosten.franchiseanteil") }, chf(z.franchise_anteil)),
          el("td", { class: "zahl", "data-label": t("kosten.selbstbehalt") }, chf(z.selbstbehalt)),
          el("td", { class: "zahl stark", "data-label": t("kosten.total") }, chf(z.total))]
        : el("td", { colspan: 5, class: "hilfe" }, t("kosten.nicht_verfuegbar")),
    )));
  } catch (e) {
    $("#kosten-text").textContent = t("allgemein.fehler", { fehler: e.message });
  }
}

// ------------------------------------------------------------------ Dialoge
function bindeDialoge() {
  document.querySelectorAll("dialog").forEach((d) => {
    d.addEventListener("click", (e) => {
      if (e.target === d || e.target.closest("[data-close]")) d.close();
    });
  });
  $("#anfrage-form").addEventListener("submit", sendeAnfrage);
}

async function zeigeVersicherer(bagNr) {
  const d = $("#dlg-versicherer");
  const inhalt = $("#vers-inhalt");
  inhalt.replaceChildren(el("p", {}, t("allgemein.laden")));
  d.showModal();
  try {
    const v = await api(`/api/versicherer/${bagNr}`);
    $("#vers-titel").textContent = v.name_kurz;
    const adresse = [v.strasse, v.postfach, [v.plz, v.ort].filter(Boolean).join(" ")].filter(Boolean);
    const taet = v.taetigkeit.length >= 26 && v.taetigkeit.every((x) => x.region_nr === null)
      ? t("versicherer.ganze_schweiz")
      : v.taetigkeit.map((x) => (x.region_nr === null ? x.kanton : `${x.kanton} (Region ${x.region_nr})`)).join(", ");
    const zeile = (label, wert) => (wert ? [el("dt", {}, label), el("dd", {}, wert)] : []);
    const namen = [...new Set([v.name_de, v.name_fr, v.name_it].filter(Boolean))];
    inhalt.replaceChildren(el("dl", { class: "details" },
      zeile(t("versicherer.namen"), namen.map((n) => el("div", {}, n))),
      zeile(t("versicherer.adresse"), adresse.map((n) => el("div", {}, n))),
      zeile(t("versicherer.telefon"), v.tel && el("a", { href: `tel:${v.tel.replace(/\s/g, "")}` }, v.tel)),
      zeile(t("versicherer.email"), v.email && el("a", { href: `mailto:${v.email}` }, v.email)),
      zeile(t("versicherer.web"), v.web && el("a", { href: v.web, target: "_blank", rel: "noopener" }, v.web.replace(/^https?:\/\//, ""))),
      zeile(t("versicherer.rechtsform"), v.rechtsform),
      zeile(t("versicherer.gruppe"), v.gruppe),
      zeile(t("versicherer.bagnr"), String(v.bag_nr)),
      zeile(t("versicherer.taetigkeit"), taet),
      zeile(t("versicherer.modelle"), el("ul", {}, v.tarife.map((x) => el("li", {}, `${x.modell}: ${x.bezeichnung_de}`)))),
    ));
  } catch (e) {
    inhalt.replaceChildren(el("p", { class: "meldung fehler" }, t("allgemein.fehler", { fehler: e.message })));
  }
}

function oeffneAnfrage(auswahl) {
  const r = state.resultat;
  state.anfrage = auswahl;
  const haushalt = r.personen.length > 1;
  const g = r.kontext.gemeinde;
  const kopf = auswahl.modus === "gemeinsam"
    ? el("p", {}, el("strong", {}, auswahl.angebot.versicherer), ` · ${auswahl.angebot.modell} – ${auswahl.angebot.tarifbezeichnung}`)
    : el("p", {}, el("strong", {}, t("modus.kombination")),
      ` · ${t("kombination.versicherer", { anzahl: new Set(auswahl.zeilen.map((z) => z.bag_nr)).size })}`);
  $("#anfrage-wahl").replaceChildren(
    el("h3", {}, t("anfrage.wahl")),
    kopf,
    el("p", { class: "preis" }, chf(auswahl.monat),
      el("small", {}, ` ${t("pro_monat")} · ${chf(rund(auswahl.monat * 12))} ${t("pro_jahr")}${haushalt ? ` · ${t("anfrage.total_personen", { anzahl: r.personen.length })}` : ""}`)),
    haushalt ? el("ul", { class: "wahl-personen" }, r.personen.map((p, i) => el("li", {},
      `${p.name} (${p.geburtsjahr}): `,
      auswahl.modus === "kombination" ? `${auswahl.zeilen[i].versicherer} – ${auswahl.zeilen[i].tarifbezeichnung}, ` : "",
      el("strong", {}, chf(auswahl.zeilen[i].monat))))) : null,
    el("p", { class: "hilfe" }, `${g.gemeinde} (${g.kanton}, Region ${g.region_nr})`),
  );

  // Namen der versicherten Personen (vorausgefüllt aus dem Rechner, Pflicht)
  const liste = $("#anfrage-personen");
  liste.replaceChildren(...r.personen.map((p, i) => {
    const vorgabe = $(".p-name", state.personen[i].el).value.trim();
    return el("div", { class: "feld" },
      el("label", { for: `a-person-${i}` }, `${t("anfrage.person.name", { n: i + 1 })} (${p.geburtsjahr}) *`),
      el("input", { id: `a-person-${i}`, class: "a-person-name", required: true, maxlength: 80, value: vorgabe || null,
        placeholder: t("form.person.name.platzhalter"), autocomplete: "off" }));
  }));

  // Kontaktperson aus der ersten Person vorschlagen
  const erster = $(".p-name", state.personen[0].el).value.trim().split(/\s+/);
  if (erster[0] && !$("#a-vorname").value) $("#a-vorname").value = erster[0];
  if (erster.length > 1 && !$("#a-nachname").value) $("#a-nachname").value = erster.slice(1).join(" ");

  $("#anfrage-fehler").hidden = true;
  $("#anfrage-formular-bereich").hidden = false;
  $("#anfrage-danke").hidden = true;
  $("#dlg-anfrage").showModal();
  const leer = $$(".a-person-name").find((n) => !n.value) || $("#a-vorname");
  leer.focus();
}

async function sendeAnfrage(e) {
  e.preventDefault();
  const form = e.target;
  const fehler = $("#anfrage-fehler");
  if (!form.checkValidity()) {
    fehler.textContent = t("anfrage.pflicht");
    fehler.hidden = false;
    form.reportValidity();
    return;
  }
  const f = Object.fromEntries(new FormData(form));
  const p = eingaben();
  const namen = $$(".a-person-name").map((n) => n.value.trim());
  const body = {
    bfs_nr: p.bfs_nr,
    modell: p.modell,
    kinderrabatt: p.kinderrabatt,
    modus: state.anfrage.modus,
    aktueller_versicherer: p.aktueller_versicherer,
    aktuelle_praemie: p.aktuelle_praemie,
    personen: p.personen.map((x, i) => ({ ...x, name: namen[i], tarif_id: state.anfrage.zeilen[i].tarif_id })),
    einwilligung: $("#a-einwilligung").checked,
    website: f.website || null,
    kontakt: {
      anrede: f.anrede, vorname: f.vorname, nachname: f.nachname, email: f.email, telefon: f.telefon,
      strasse: f.strasse || null, geburtsdatum: f.geburtsdatum || null,
      erreichbarkeit: f.erreichbarkeit || null, bemerkung: f.bemerkung || null,
    },
  };
  const knopf = $("#anfrage-senden");
  knopf.disabled = true;
  knopf.textContent = t("anfrage.sendet");
  try {
    const r = await post("/api/anfrage", body);
    // Namen in den Rechner übernehmen
    namen.forEach((n, i) => { if (state.personen[i]) $(".p-name", state.personen[i].el).value = n; });
    $("#anfrage-formular-bereich").hidden = true;
    $("#anfrage-danke-text").textContent = t("anfrage.danke.text", { id: r.lead_id });
    $("#anfrage-danke").hidden = false;
    form.reset();
  } catch (err) {
    fehler.textContent = t("anfrage.fehler", { fehler: err.message });
    fehler.hidden = false;
  } finally {
    knopf.disabled = false;
    knopf.textContent = t("anfrage.senden");
  }
}

// ------------------------------------------------------------------ URL-Zustand (teilbare Links)
// p=Name~Jahrgang~Unfall(1/0)~Franchise, mehrfach für Haushalte
function zustandInUrl(p) {
  const u = new URLSearchParams();
  u.set("bfs", p.bfs_nr);
  p.personen.forEach((x) => u.append("p", [x.name, x.geburtsjahr, x.mit_unfall ? 1 : 0, x.franchise].join("~")));
  if (p.modell) u.set("modell", p.modell);
  if (!p.kinderrabatt) u.set("kinderrabatt", "0");
  if (p.aktueller_versicherer) u.set("aktuell", p.aktueller_versicherer);
  if (p.aktuelle_praemie) u.set("praemie", p.aktuelle_praemie);
  if (p.personen.length > 1 && state.modus !== "gemeinsam") u.set("modus", state.modus);
  if (SPRACHE !== "de") u.set("lang", SPRACHE);
  history.replaceState(null, "", `?${u}`);
}

async function zustandAusUrl() {
  const u = new URLSearchParams(location.search);
  if (!u.get("bfs") || !u.getAll("p").length) return false;
  try {
    const g = await api(`/api/gemeinden/${u.get("bfs")}`);
    const treffer = await api("/api/orte", { q: String(g.plz[0]) });
    const gem = treffer.gemeinden.find((x) => x.bfs_nr === g.bfs_nr);
    if (!gem) return false;
    if (u.get("modell")) $("#modell").value = u.get("modell");
    if (u.get("kinderrabatt") === "0") $("#kinderrabatt").checked = false;
    if (u.get("aktuell")) $("#aktueller-versicherer").value = u.get("aktuell");
    if (u.get("praemie")) {
      $("#aktuelle-praemie").value = u.get("praemie");
    }
    if (u.get("aktuell") || u.get("praemie")) $("details.aktuell").open = true;
    if (u.get("modus") === "kombination") state.modus = "kombination";
    await Promise.all(u.getAll("p").slice(0, MAX_PERSONEN).map((s) => {
      const [name, jahr, unfall, franchise] = s.split("~");
      return personHinzufuegen({ name, geburtsjahr: jahr, unfall: unfall !== "0", franchise });
    }));
    waehleGemeinde(gem);
    return true;
  } catch (e) {
    console.warn("URL-Zustand ignoriert:", e);
    return false;
  }
}

init().catch((e) => {
  document.body.prepend(el("p", { class: "meldung fehler" }, `Initialisierung fehlgeschlagen: ${e.message}`));
});
