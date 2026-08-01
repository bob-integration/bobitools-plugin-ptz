// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
//
// Banc d'essai du montage de l'UI, sans navigateur.
//
//     node mount_check.js .        (depuis le dossier du plugin)
//
// À passer après TOUTE modification de page.js ou page.html.
//
// Motif : deux régressions d'affilée (fonction supprimée par une réécriture de bloc,
// identifiant absent du HTML) sont passées à travers `node --check`, qui ne vérifie que la
// syntaxe. Ici on EXÉCUTE réellement mount() contre un DOM minimal construit à partir des
// identifiants réellement présents dans page.html : toute référence à un élément absent ou
// à une fonction disparue lève, donc se voit.
//
// Ce n'est pas un rendu : ça ne dit rien de l'apparence. Ça dit seulement que le code se
// monte sans exploser — exactement la classe de panne qu'on vient de subir deux fois.
const fs = require("fs");
const path = process.argv[2];

const html = fs.readFileSync(path + "/page.html", "utf8");
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
const classes = new Set([...html.matchAll(/class="([^"]+)"/g)]
    .flatMap((m) => m[1].split(/\s+/)).filter(Boolean));

const calls = [];
const handlers = [];   // on rejouera les clics : c'est là que les régressions se voient
function makeEl(desc) {
    const el = {
        _desc: desc, dataset: {}, style: {}, hidden: false, checked: false,
        disabled: false, value: "", textContent: "", innerHTML: "", placeholder: "",
        maxLength: 0, id: "", className: "", title: "",
        classList: { toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false },
        addEventListener: (ev, fn) => { calls.push(desc + ":" + ev); handlers.push({ desc, ev, fn }); },
        querySelector: (s) => query(s),
        querySelectorAll: (s) => queryAll(s),
        appendChild: () => {}, closest: () => null, focus: () => {}, remove: () => {},
        getAttribute: () => null, setAttribute: () => {}, matches: () => false,
    };
    return el;
}
function query(sel) {
    if (sel.startsWith("#")) return ids.has(sel.slice(1)) ? makeEl(sel) : null;
    return makeEl(sel);
}
function queryAll(sel) {
    // Deux éléments : assez pour exercer les boucles sans exploser la sortie.
    return [makeEl(sel + "[0]"), makeEl(sel + "[1]")];
}

global.window = {};
global.document = {
    querySelector: query, querySelectorAll: queryAll,
    createElement: (t) => makeEl("<" + t + ">"),
};
global.BT = { esc: (s) => String(s == null ? "" : s), toast: () => {} };
global.window.BT = global.BT;

// Charge le module
const src = fs.readFileSync(path + "/page.js", "utf8");
eval(src);

const tool = global.window.BTTools && global.window.BTTools.ptz;
if (!tool) { console.error("✗ window.BTTools.ptz non défini"); process.exit(1); }

// ctx minimal : l'API renvoie des formes plausibles pour que le rendu s'exécute.
const shapes = {
    cameras: { cameras: [{ id: "a1", name: "Cam", driver: "panasonic_aw", host: "10.0.0.1",
                           model: "AW-UE160", identity: { model: "AW-UE160" }, capabilities: ["params", "presets", "console", "power", "preset_names"],
                           outputs: [{ value: "", label: "Général" }, { value: "12g", label: "12G" }],
                           preset_range: [1, 100], reachable: true, enabled: true }], groups: [] },
    drivers: { drivers: [{ kind: "panasonic_aw", label: "Panasonic", models: [], available: true, default_port: 80 }], poll_interval: 10 },
    snapshots: { snapshots: [] },
    overview: { columns: [], rows: [] },
    "presets/matrix": { cameras: [], range: [1, 100] },
};
const ctx = {
    t: (k) => k,
    toast: () => {},
    api: (p) => Promise.resolve(shapes[p] || shapes[p.split("?")[0]] ||
        { values: {}, schema: [], outputs: [], presets: [], names: {}, on_device: true, lo: 1, hi: 100 }),
};

let failed = false;
process.on("unhandledRejection", (e) => { console.error("✗ promesse rejetée :", e.message); failed = true; });

try {
    tool.mount(makeEl("#root"), ctx);
    console.log("✓ mount() sans erreur ·", calls.length, "écouteurs posés");
} catch (e) {
    console.error("✗ mount() a levé :", e.message);
    console.error(e.stack.split("\n").slice(1, 4).join("\n"));
    failed = true;
}

// Rejoue TOUS les gestionnaires de clic et de changement. Une fonction supprimée par une
// réécriture, un identifiant absent du HTML : ça lève ici, pas au montage.
setTimeout(() => {
    let ko = 0;
    // Instantané AVANT de rejouer : un gestionnaire qui re-rend la vue pose de nouveaux
    // écouteurs, et parcourir un tableau qui s'allonge ne se termine jamais.
    const snapshot = handlers.slice();
    for (const h of snapshot) {
        if (h.ev !== "click" && h.ev !== "change" && h.ev !== "input") continue;
        try { h.fn({ target: makeEl("evt"), preventDefault: () => {}, stopPropagation: () => {} }); }
        catch (e) { console.error("✗ " + h.ev + " sur " + h.desc + " → " + e.message); ko++; failed = true; }
    }
    console.log((ko ? "✗ " : "✓ ") + handlers.length + " gestionnaires rejoués · " + ko + " en erreur");

    try { tool.unmount(); console.log("✓ unmount() sans erreur"); }
    catch (e) { console.error("✗ unmount() a levé :", e.message); failed = true; }
    process.exit(failed ? 1 : 0);
}, 300);
