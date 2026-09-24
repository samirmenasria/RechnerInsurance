// Prämienrechner OKP – Frontend (ohne Build-Schritt, ES-Module)

const SPRACHE = (new URLSearchParams(location.search).get("lang") || "de").slice(0, 2);
const SEITE = 25; // Zeilen vor «alle anzeigen»

const state = {
  texte: {},
  stamm: null,
  meta: null,
  gemeinde: null,        // gewählte Gemeinde (Objekt aus /api/orte)
  altersklasse: null,    // {code, name, alter, praemienjahr}
  resultat: null,
  alleZeigen: false,
  anfrageAngebot: null,
};

// ------------------------------------------------------------------ Hilfsfunktionen
const $ = (sel, root = document) => root.querySelector(sel);
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
const diff = (v) => (v === null || v === undefined ? "–" : v === 0 ? "–" : `${v > 0 ? "+" : "−"}${fmt.format(Math.abs(v))}`);

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

  // Modelle
  const modell = $("#modell");
  modell.append(el("option", { value: "alle" }, t("form.modell.alle")));
  state.stamm.modelle.forEach((m) => modell.append(el("option", { value: m.code }, m.name_de)));

  // Versicherer (aktuell)
  const akt = $("#aktueller-versicherer");
  akt.append(el("option", { value: "" }, t("form.aktuell.keiner")));
  state.stamm.versicherer.forEach((v) => akt.append(el("option", { value: v.bag_nr }, v.name_kurz)));

  bindeOrtssuche();
  bindeFormular();
  bindeKosten();
  bindeDialoge();
  zustandAusUrl();
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
    // Eindeutige Gemeinde bei vollständiger PLZ -> direkt übernehmen
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
        role: "option", id: `ort-opt-${i}`, "data-index": i,
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

function waehleGemeinde(g) {
  state.gemeinde = g;
  $("#ort-liste").hidden = true;
  $("#ort-meldung").hidden = true;
  $("#ort").value = `${g.plz[0]} ${g.gemeinde}`;
  $("#ort-suche").hidden = true;
  $("#ort-gewaehlt-text").textContent = t("form.ort.gewaehlt", { gemeinde: g.gemeinde, kanton: g.kanton, region: g.region_nr });
  $("#ort-gewaehlt").hidden = false;
  berechnenWennBereit();
}

// ------------------------------------------------------------------ Formular
function bindeFormular() {
  const gj = $("#geburtsjahr");
  gj.addEventListener("input", debounce(aktualisiereAltersklasse, 250));
  ["#franchise", "#modell", "#kinderrabatt", "#aktueller-versicherer"].forEach((s) =>
    $(s).addEventListener("change", berechnenWennBereit));
  $("#aktuelle-praemie").addEventListener("input", debounce(berechnenWennBereit, 400));
  document.querySelectorAll("input[name=unfall]").forEach((n) => n.addEventListener("change", berechnenWennBereit));
  $("#rechner").addEventListener("submit", (e) => { e.preventDefault(); berechnen(); });
  $("#filter").addEventListener("input", zeichneTabelle);
  $("#mehr").addEventListener("click", () => { state.alleZeigen = !state.alleZeigen; zeichneTabelle(); });
}

async function aktualisiereAltersklasse() {
  const jahr = parseInt($("#geburtsjahr").value, 10);
  const info = $("#geburtsjahr-info");
  const franchise = $("#franchise");
  if (!jahr || jahr < 1900 || jahr > state.meta.praemienjahr) {
    state.altersklasse = null;
    info.textContent = $("#geburtsjahr").value.length >= 4 ? t("form.geburtsjahr.fehler") : "";
    franchise.disabled = true;
    return;
  }
  try {
    const akl = await api("/api/altersklasse", { geburtsjahr: jahr });
    const alt = state.altersklasse?.code;
    state.altersklasse = akl;
    info.textContent = t("form.geburtsjahr.info", { alter: akl.alter, jahr: akl.praemienjahr, klasse: akl.name });
    $("#kinderrabatt-feld").hidden = akl.code !== "AKL-KIN";
    if (alt !== akl.code) fuelleFranchisen(akl.code);
    berechnenWennBereit();
  } catch (e) {
    state.altersklasse = null;
    info.textContent = e.message;
  }
}

function fuelleFranchisen(code, gewuenscht) {
  const klasse = state.stamm.altersklassen.find((a) => a.code === code);
  const sel = $("#franchise");
  const bisher = gewuenscht || sel.value;
  sel.replaceChildren(...klasse.franchisen.map((f) =>
    el("option", { value: f.code }, `CHF ${fmt0.format(f.betrag)}${f.ist_ordentlich ? ` (${t("form.franchise.ordentlich")})` : ""}`)));
  const ordentlich = klasse.franchisen.find((f) => f.ist_ordentlich)?.code;
  sel.value = klasse.franchisen.some((f) => f.code === bisher) ? bisher : ordentlich;
  sel.disabled = false;
}

function eingaben() {
  return {
    bfs: state.gemeinde?.bfs_nr,
    geburtsjahr: parseInt($("#geburtsjahr").value, 10),
    unfall: document.querySelector("input[name=unfall]:checked").value,
    franchise: $("#franchise").value,
    modell: $("#modell").value,
    kinderrabatt: state.altersklasse?.code === "AKL-KIN" && $("#kinderrabatt").checked,
    aktueller_versicherer: $("#aktueller-versicherer").value || null,
    aktuelle_praemie: $("#aktuelle-praemie").value || null,
  };
}

const bereit = () => state.gemeinde && state.altersklasse && $("#franchise").value;

function berechnenWennBereit() { if (bereit()) berechnen(); }

async function berechnen() {
  if (!bereit()) {
    if (!state.gemeinde) $("#ort").focus();
    else $("#geburtsjahr").focus();
    return;
  }
  const p = eingaben();
  try {
    state.resultat = await api("/api/praemien", p);
    state.alleZeigen = false;
    zeichneResultat();
    ladeKosten();
    zustandInUrl(p);
  } catch (e) {
    $("#resultat").hidden = true;
    $("#resultat-leer").hidden = false;
    $("#resultat-leer").replaceChildren(el("p", { class: "meldung fehler" }, t("allgemein.fehler", { fehler: e.message })));
  }
}

// ------------------------------------------------------------------ Resultate
function zeichneResultat() {
  const r = state.resultat;
  const k = r.kontext;
  $("#resultat-leer").hidden = true;
  $("#resultat").hidden = false;
  $("#resultat-titel").textContent = t("resultat.titel", { anzahl: r.anzahl });
  $("#resultat-kontext").textContent = t("resultat.kontext", {
    gemeinde: k.gemeinde.gemeinde, kanton: k.gemeinde.kanton, region: k.gemeinde.region_nr,
    klasse: state.altersklasse.name, franchise: `CHF ${fmt0.format(k.franchise_betrag)}`,
    unfall: k.mit_unfall ? t("form.unfall.ja") : t("form.unfall.nein"),
  });

  const kz = $("#kennzahlen");
  kz.replaceChildren();
  if (r.angebote.length) {
    const g = r.angebote[0];
    kz.append(el("div", { class: "kennzahl" },
      el("span", {}, t("resultat.guenstigste")),
      el("strong", {}, chf(g.monat), el("small", {}, ` ${t("pro_monat")}`)),
      el("small", {}, `${g.versicherer} · ${g.tarifbezeichnung}`)));
  }
  if (r.aktuell) {
    const a = r.aktuell;
    kz.append(el("div", { class: "kennzahl" },
      el("span", {}, t("resultat.aktuell")),
      el("strong", {}, chf(a.monat), el("small", {}, ` ${t("pro_monat")}`)),
      el("small", {}, a.versicherer ? `${a.versicherer} · ${a.tarifbezeichnung}` : "")));
    if (r.angebote.length) {
      const ersparnis = Math.max(0, (a.monat - r.angebote[0].monat) * 12);
      kz.append(el("div", { class: "kennzahl gut" },
        el("span", {}, t("resultat.ersparnis")), el("strong", {}, chf(ersparnis))));
    }
  } else if (r.hinweis_aktuell) {
    kz.append(el("p", { class: "hilfe" }, r.hinweis_aktuell));
  }

  document.querySelectorAll(".spalte-aktuell").forEach((n) => { n.hidden = !r.aktuell; });
  zeichneTabelle();

  // Tarifauswahl für Gesamtkosten
  const sel = $("#kosten-tarif");
  const bisher = sel.value;
  sel.replaceChildren(el("option", { value: "" }, t("kosten.angebot.guenstigstes")),
    ...r.angebote.slice(0, 50).map((a) => el("option", { value: a.tarif_id }, `${a.versicherer} – ${a.tarifbezeichnung}`)));
  if ([...sel.options].some((o) => o.value === bisher)) sel.value = bisher;
}

function zeichneTabelle() {
  const r = state.resultat;
  if (!r) return;
  const q = $("#filter").value.trim().toLowerCase();
  let zeilen = r.angebote.filter((a) => !q || `${a.versicherer} ${a.tarifbezeichnung} ${a.modell}`.toLowerCase().includes(q));
  const total = zeilen.length;
  if (!state.alleZeigen && !q) zeilen = zeilen.slice(0, SEITE);

  const body = $("#tabelle tbody");
  body.replaceChildren(...zeilen.map((a) => el("tr", { class: a.ist_aktuell ? "ist-aktuell" : null },
    el("td", { "data-label": t("spalte.rang") }, a.rang),
    el("td", { "data-label": t("spalte.versicherer") },
      el("button", { type: "button", class: "link", onclick: () => zeigeVersicherer(a.bag_nr) }, a.versicherer),
      a.ist_aktuell ? el("span", { class: "badge" }, t("zeile.aktuell")) : null),
    el("td", { "data-label": t("spalte.modell") },
      el("span", { class: `modell modell-${a.tariftyp.toLowerCase()}` }, a.modell), " ",
      a.tarifbezeichnung,
      a.rabatt ? el("span", { class: "badge rabatt" }, t("zeile.rabatt", { code: a.altersuntergruppe })) : null),
    el("td", { class: "zahl", "data-label": t("spalte.praemie") },
      el("strong", {}, chf(a.monat)), el("small", { class: "unter" }, `${chf(a.jahr)} ${t("pro_jahr")}`)),
    el("td", { class: "zahl", "data-label": t("spalte.diff_guenstigste") }, diff(a.diff_guenstigste_monat)),
    el("td", { class: `zahl spalte-aktuell ${a.diff_aktuell_monat < 0 ? "gut" : a.diff_aktuell_monat > 0 ? "schlecht" : ""}`,
      "data-label": t("spalte.diff_aktuell"), hidden: !r.aktuell }, diff(a.diff_aktuell_monat)),
    el("td", { class: "aktion" },
      el("button", { type: "button", class: "primaer klein", title: t("anfrage.titel"), onclick: () => oeffneAnfrage(a) }, t("zeile.anfragen"))),
  )));
  $("#keine-angebote").hidden = total > 0;
  const mehr = $("#mehr");
  mehr.hidden = !!q || total <= SEITE;
  mehr.textContent = state.alleZeigen ? t("resultat.weniger") : t("resultat.alle", { anzahl: total });
}

// ------------------------------------------------------------------ Gesamtkosten
function bindeKosten() {
  const betrag = $("#kosten-betrag");
  const slider = $("#kosten-slider");
  const neu = debounce(ladeKosten, 250);
  betrag.addEventListener("input", () => { slider.value = betrag.value; neu(); });
  slider.addEventListener("input", () => { betrag.value = slider.value; neu(); });
  $("#kosten-tarif").addEventListener("change", ladeKosten);
}

async function ladeKosten() {
  if (!state.resultat) return;
  const p = eingaben();
  const kosten = Math.max(0, parseFloat($("#kosten-betrag").value) || 0);
  try {
    const r = await api("/api/gesamtkosten", {
      bfs: p.bfs, geburtsjahr: p.geburtsjahr, unfall: p.unfall, modell: p.modell,
      kinderrabatt: p.kinderrabatt, kosten, tarif_id: $("#kosten-tarif").value || null,
    });
    $("#kosten-text").textContent = t("kosten.text", { max: fmt0.format(r.max_selbstbehalt) });
    $("#kosten-tabelle tbody").replaceChildren(...r.zeilen.map((z) => el("tr", { class: z.optimal ? "optimal" : null },
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
      ? "ganze Schweiz"
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

function oeffneAnfrage(angebot) {
  state.anfrageAngebot = angebot;
  const k = state.resultat.kontext;
  $("#anfrage-wahl").replaceChildren(
    el("h3", {}, t("anfrage.wahl")),
    el("p", {}, el("strong", {}, angebot.versicherer), ` · ${angebot.modell} – ${angebot.tarifbezeichnung}`),
    el("p", { class: "preis" }, chf(angebot.monat), el("small", {}, ` ${t("pro_monat")} · ${chf(angebot.jahr)} ${t("pro_jahr")}`)),
    el("p", { class: "hilfe" }, t("resultat.kontext", {
      gemeinde: k.gemeinde.gemeinde, kanton: k.gemeinde.kanton, region: k.gemeinde.region_nr,
      klasse: state.altersklasse.name, franchise: `CHF ${fmt0.format(k.franchise_betrag)}`,
      unfall: k.mit_unfall ? t("form.unfall.ja") : t("form.unfall.nein"),
    })),
  );
  $("#anfrage-fehler").hidden = true;
  $("#anfrage-formular-bereich").hidden = false;
  $("#anfrage-danke").hidden = true;
  $("#dlg-anfrage").showModal();
  $("#a-vorname").focus();
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
  const body = {
    bfs_nr: p.bfs,
    geburtsjahr: p.geburtsjahr,
    mit_unfall: p.unfall === "true",
    franchise: p.franchise,
    modell: p.modell === "alle" ? null : p.modell,
    kinderrabatt: p.kinderrabatt,
    tarif_id: state.anfrageAngebot.tarif_id,
    aktueller_versicherer: p.aktueller_versicherer ? parseInt(p.aktueller_versicherer, 10) : null,
    aktuelle_praemie: p.aktuelle_praemie ? parseFloat(p.aktuelle_praemie) : null,
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
    const r = await api("/api/anfrage", null, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
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
function zustandInUrl(p) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) if (v !== null && v !== undefined && v !== "" && v !== false) u.set(k, v);
  history.replaceState(null, "", `?${u}`);
}

async function zustandAusUrl() {
  const u = new URLSearchParams(location.search);
  if (!u.get("bfs") || !u.get("geburtsjahr")) return;
  try {
    const g = await api(`/api/gemeinden/${u.get("bfs")}`);
    const treffer = await api("/api/orte", { q: String(g.plz[0]) });
    const gem = treffer.gemeinden.find((x) => x.bfs_nr === g.bfs_nr);
    if (!gem) return;
    $("#geburtsjahr").value = u.get("geburtsjahr");
    if (u.get("unfall") === "false") document.querySelector("input[name=unfall][value=false]").checked = true;
    if (u.get("modell")) $("#modell").value = u.get("modell");
    if (u.get("kinderrabatt") === "true") $("#kinderrabatt").checked = true;
    if (u.get("aktueller_versicherer")) $("#aktueller-versicherer").value = u.get("aktueller_versicherer");
    if (u.get("aktuelle_praemie")) $("#aktuelle-praemie").value = u.get("aktuelle_praemie");
    const akl = await api("/api/altersklasse", { geburtsjahr: u.get("geburtsjahr") });
    state.altersklasse = akl;
    $("#geburtsjahr-info").textContent = t("form.geburtsjahr.info", { alter: akl.alter, jahr: akl.praemienjahr, klasse: akl.name });
    $("#kinderrabatt-feld").hidden = akl.code !== "AKL-KIN";
    fuelleFranchisen(akl.code, u.get("franchise"));
    waehleGemeinde(gem);
  } catch (e) {
    console.warn("URL-Zustand ignoriert:", e);
  }
}

init().catch((e) => {
  document.body.prepend(el("p", { class: "meldung fehler" }, `Initialisierung fehlgeschlagen: ${e.message}`));
});
