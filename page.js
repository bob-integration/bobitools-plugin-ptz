// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// UI de « Caméras tourelles ». Front pur : toute la logique tourne dans le conteneur,
// atteinte via ctx.api(...).
//
// L'UI est GÉNÉRIQUE par construction : elle ne cite aucune marque et ne teste aucun
// modèle. Ce qu'elle affiche vient de deux sources renvoyées par le pilote :
//   - `capabilities` : quels onglets et quelles actions ont un sens pour cette caméra ;
//   - `schema` des paramètres : quels contrôles construire, et de quel type.
// Ajouter une marque côté serveur suffit donc à la voir apparaître ici.
window.BTTools = window.BTTools || {};
window.BTTools.ptz = (function () {
    "use strict";
    let ctx = null, root = null;
    let cams = [], catalog = [], groups = [], pollInterval = 0;
    let selected = new Set();          // sélection multiple → actions groupées
    let selId = null;                  // caméra affichée dans le détail
    let tab = "params";
    let view = "fleet";               // vue de premier niveau affichée
    let fleetSort = "none";           // tri du parc : none | number | ip
    const viewLoaded = {};            // vues déjà chargées (chargement paresseux)
    let schemaCache = {};              // { camId: [param…] }
    let timer = null;

    const esc = (s) => (window.BT && BT.esc ? BT.esc(s) : String(s == null ? "" : s));
    const tr = (key, fb) => { const v = ctx && ctx.t ? ctx.t(key) : null; return (v && v !== key) ? v : fb; };
    const $ = (sel) => root.querySelector(sel);
    const toast = (m, k) => ctx.toast(m, k);
    const cam = (id) => cams.find((c) => c.id === id);
    const can = (c, cap) => !!c && (c.capabilities || []).indexOf(cap) >= 0;

    // ── Cycle de vie ─────────────────────────────────────────
    function mount(el, context) {
        ctx = context; root = el;
        applyI18n();
        $("#ptz-add").addEventListener("click", () => showForm());
        $("#ptz-add-batch").addEventListener("click", showBatch);
        $("#ptz-batch").addEventListener("submit", onBatchSubmit);
        $("#ptz-b-cancel").addEventListener("click", () => { $("#ptz-batch").hidden = true; });
        ["#ptz-b-host", "#ptz-b-count", "#ptz-b-prefix", "#ptz-b-first"].forEach((sel) =>
            $(sel).addEventListener("input", batchPreview));
        $("#ptz-refresh").addEventListener("click", () => refresh());
        $("#ptz-probe-all").addEventListener("click", pollNow);
        $("#ptz-discover").addEventListener("click", rediscover);
        root.querySelectorAll(".ptz-view-tab").forEach((b) =>
            b.addEventListener("click", () => setView(b.dataset.view)));
        $("#ptz-nm-refresh").addEventListener("click", loadNames);
        ["#ptz-nm-from", "#ptz-nm-to"].forEach((sel) =>
            $(sel).addEventListener("change", renderNames));
        $("#ptz-nm-named").addEventListener("change", renderNames);
        $("#ptz-ov-refresh").addEventListener("click", loadOverview);
        $("#ptz-rcp-refresh").addEventListener("click", () => loadRcp());
        $("#ptz-rcp-setref").addEventListener("click", rcpSetReference);
        $("#ptz-rcp-clearref").addEventListener("click", rcpClearReference);
        $("#ptz-rcp-exportcsv").addEventListener("click", rcpExportCsv);
        $("#ptz-rcp-diaph").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) rcpSetDiaphStyle(b.dataset.d); });
        $("#ptz-rcp-ped").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) rcpSetPedStyle(b.dataset.p); });
        $("#ptz-rcp-view").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) rcpSetViewMode(b.dataset.v); });
        $("#ptz-rcp-cmp").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) rcpSetValueMode(b.dataset.m); });
        // Glisser des rotatifs : gestionnaires globaux (le contrôle est repeint sous eux).
        document.addEventListener("mousemove", rcpOnDragMove);
        document.addEventListener("mouseup", rcpOnDragUp);
        document.addEventListener("touchmove", rcpOnDragMove, { passive: false });
        document.addEventListener("touchend", rcpOnDragUp);
        $("#ptz-f-cancel").addEventListener("click", hideForm);
        $("#ptz-form").addEventListener("submit", onFormSubmit);
        $("#ptz-f-driver").addEventListener("change", onDriverChange);
        $("#ptz-selall").addEventListener("change", onSelectAll);
        ["#ptz-sort", "#ptz-ov-sort", "#ptz-nm-sort", "#ptz-rcp-sort"].forEach((sel) => {
            const el = $(sel);
            if (el) el.addEventListener("change", (e) => setFleetSort(e.target.value));
        });
        $("#ptz-snap-new").addEventListener("click", newSnap);
        $("#ptz-snap-scopesel").addEventListener("change", () => { $("#ptz-snap-scope").textContent = snapScope(); });
        $("#ptz-bulk-clear").addEventListener("click", () => { selected.clear(); renderFleet(); });
        $("#ptz-bulk-recall").addEventListener("click", () => bulkPreset("recall"));
        $("#ptz-bulk-rename").addEventListener("click", bulkRename);
        $("#ptz-bulk-apply").addEventListener("click", bulkParams);
        $("#ptz-bulk-identify").addEventListener("click", bulkIdentify);
        $("#ptz-bulk-key").addEventListener("change", renderBulkValue);
        $("#ptz-pn-refresh").addEventListener("click", loadPanels);
        $("#ptz-gr-refresh").addEventListener("click", loadGrid);
        $("#ptz-gr-save").addEventListener("click", saveGrid);
        $("#ptz-ps-load").addEventListener("click", prestaLoad);
        $("#ptz-ps-saveas").addEventListener("click", prestaSaveAs);
        $("#ptz-ps-del").addEventListener("click", prestaDelete);
        $("#ptz-ps-export").addEventListener("click", prestaExport);
        $("#ptz-ps-import").addEventListener("click", () => { const f = $("#ptz-ps-file"); if (f && f.click) f.click(); });
        $("#ptz-ps-file").addEventListener("change", prestaImport);
        $("#ptz-ps-apply").addEventListener("click", prestaApply);
        $("#ptz-sb-new").addEventListener("click", sbNew);
        $("#ptz-sb-del").addEventListener("click", sbDelete);
        $("#ptz-sb-save").addEventListener("click", sbSave);
        $("#ptz-sb-sel").addEventListener("change", (e) => sbSelectBox(e.target.value));
        root.querySelectorAll("#ptz-sb-modes .ptz-pn-modebtn").forEach((b) =>
            b.addEventListener("click", () => sbSetMode(b.dataset.mode)));
        $("#ptz-sb-full").addEventListener("click", () => sbSetFull(true));
        $("#ptz-sb-exit").addEventListener("click", () => sbSetFull(false));
        if (document.addEventListener) document.addEventListener("fullscreenchange", sbOnFsChange);
        $("#ptz-pn-add").addEventListener("click", () => showPanelForm());
        $("#ptz-pn-cancel").addEventListener("click", hidePanelForm);
        $("#ptz-pn-form").addEventListener("submit", onPanelSubmit);
        refresh();
        // Suivi de connexion toutes les 10 s, quelle que soit la vue ouverte. On ne
        // retouche PAS le détail ni les cases en cours de saisie : seul l'état bouge.
        timer = setInterval(refreshStatus, 10000);
    }

    function unmount() {
        if (timer) clearInterval(timer);
        if (document.removeEventListener) {
            document.removeEventListener("fullscreenchange", sbOnFsChange);
            document.removeEventListener("mousemove", rcpOnDragMove);
            document.removeEventListener("mouseup", rcpOnDragUp);
            document.removeEventListener("touchmove", rcpOnDragMove);
            document.removeEventListener("touchend", rcpOnDragUp);
        }
        Object.keys(rcpTimers).forEach((k) => { clearTimeout(rcpTimers[k]); delete rcpTimers[k]; });
        timer = null; ctx = root = null;
        cams = []; catalog = []; selected = new Set(); selId = null; schemaCache = {};
        rcpData = {}; rcpErr = {}; rcpRef = null; rcpRefDate = null; rcpRefId = null; rcpPinsId = null;
        rcpStoreOk = null; rcpDrag = null; rcpDragActive = false; rcpRenderPending = false;
    }

    function applyI18n() {
        if (!ctx.t) return;
        root.querySelectorAll("[data-i18n]").forEach((el) => {
            const v = ctx.t(el.getAttribute("data-i18n"));
            if (v && v !== el.getAttribute("data-i18n")) el.textContent = v;
        });
    }

    // ── Chargement ───────────────────────────────────────────
    // Le parc a changé : les vues transverses ne reflètent plus la réalité.
    const invalidateViews = () => { delete viewLoaded.overview; delete viewLoaded.names; delete viewLoaded.rcp; };

    async function refresh() {
        try {
            const [cs, ds] = await Promise.all([ctx.api("cameras"), ctx.api("drivers")]);
            cams = (cs && cs.cameras) || [];
            groups = (cs && cs.groups) || [];
            catalog = (ds && ds.drivers) || [];
            pollInterval = (ds && ds.poll_interval) || 0;
        } catch (e) { toast(e.message, "error"); return; }
        $("#ptz-poll").textContent = pollInterval
            ? tr("plugin.ptz.polling", "surveillance toutes les") + " " + pollInterval + " s" : "";
        renderFleet();
        if (selId && cam(selId)) renderDetail(); else { selId = null; renderDetailEmpty(); }
    }

    // Suivi de connexion. `GET /cameras` ne touche PAS aux caméras : il rend l'état
    // relevé par la surveillance de fond du conteneur. C'est donc un rafraîchissement
    // très léger, qu'on peut se permettre toutes les dix secondes quelle que soit la vue.
    async function refreshStatus() {
        let cs;
        try { cs = await ctx.api("cameras"); }
        catch (e) { return; }          // silencieux : c'est un rafraîchissement de fond
        const before = {};
        cams.forEach((c) => { before[c.id] = c.reachable; });
        cams = (cs && cs.cameras) || [];
        groups = (cs && cs.groups) || [];

        if (view === "fleet") { renderFleet(); renderDetailHeader(); }
        else applyStatusDots();

        // Une caméra qui REVIENT doit se remplir toute seule : c'est le cas où l'on
        // attend justement que l'information remonte sans avoir à recharger la page.
        cams.forEach((c) => {
            if (c.reachable !== true || before[c.id] === true) return;
            if (view === "overview" && (ovErr[c.id] || !ovData[c.id])) {
                delete ovErr[c.id];
                ctx.api("cameras/" + c.id + "/overview")
                    .then((d) => { ovData[c.id] = d; fillOverviewRow(c.id); })
                    .catch((e) => { ovErr[c.id] = e.message; fillOverviewRow(c.id); });
            }
            if (view === "names" && (nmErr[c.id] || !nmData[c.id])) {
                delete nmErr[c.id];
                ctx.api("cameras/" + c.id + "/names")
                    .then((d) => { nmData[c.id] = d; renderNames(); })
                    .catch((e) => { nmErr[c.id] = e.message; fillNamesColumn(c.id); });
            }
            if (view === "rcp" && (rcpErr[c.id] || !rcpData[c.id])) {
                delete rcpErr[c.id];
                ctx.api("cameras/" + c.id + "/params")
                    .then((d) => { rcpData[c.id] = rcpColorOf(d); renderRcp(); })
                    .catch((e) => { rcpErr[c.id] = e.message; renderRcp(); });
            }
        });
    }

    // Pastilles d'état, partout où une caméra est représentée (lignes de la vue
    // d'ensemble, en-têtes de la matrice). Trois états distincts, comme sur les cartes :
    // joignable, injoignable, pas encore sondée.
    function applyStatusDots() {
        if (!root) return;
        const byId = {};
        cams.forEach((c) => { byId[c.id] = c; });
        root.querySelectorAll("[data-dot-for]").forEach((el) => {
            const c = byId[el.dataset.dotFor];
            const cls = !c ? "" : c.reachable === true ? "ok" : c.reachable === false ? "err" : "";
            el.className = "ptz-dot" + (cls ? " " + cls : "");
            el.title = c && c.error ? c.error : "";
        });
        // Une caméra tombée pendant qu'on regarde : sa ligne/colonne est grisée, mais les
        // dernières valeurs lues RESTENT affichées — les effacer perdrait l'information
        // sans rien apprendre. L'infobulle dit qu'elles datent d'avant la coupure.
        root.querySelectorAll("[data-row-for]").forEach((el) => {
            const c = byId[el.dataset.rowFor];
            el.classList.toggle("ptz-row-stale", !!c && c.reachable === false && !ovErr[c.id]);
        });
        root.querySelectorAll("[data-col-for]").forEach((el) => {
            const c = byId[el.dataset.colFor];
            el.classList.toggle("ptz-row-stale", !!c && c.reachable === false && !nmErr[c.id]);
        });
    }

    async function pollNow() {
        const b = $("#ptz-probe-all");
        b.disabled = true;
        try {
            const r = await ctx.api("poll", { body: {} });
            cams = (r && r.cameras) || cams;
            renderFleet(); renderDetailHeader();
        } catch (e) { toast(e.message, "error"); }
        finally { b.disabled = false; }
    }

    // ── Parc ─────────────────────────────────────────────────
    function renderFleet() {
        const box = $("#ptz-cards");
        if (!cams.length) {
            box.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.none",
                "Aucune caméra — utilisez « + Ajouter une caméra »."))}</div>`;
        } else {
            box.innerHTML = sortedCams().map(cardHtml).join("");
            box.querySelectorAll(".ptz-card").forEach((el) => {
                const id = el.dataset.id;
                el.addEventListener("click", (e) => {
                    if (e.target.closest(".ptz-pick")) return;      // la case gère elle-même
                    const act = e.target.closest("[data-a]");
                    if (act) { e.stopPropagation(); return cardAction(act.dataset.a, id); }
                    select(id);
                });
                el.querySelector(".ptz-pick").addEventListener("change", (e) => {
                    if (e.target.checked) selected.add(id); else selected.delete(id);
                    renderFleet();
                });
            });
        }
        const reach = cams.filter((c) => c.reachable === true).length;
        $("#ptz-count").textContent = cams.length
            ? `${reach}/${cams.length} ${tr("plugin.ptz.reachable", "joignables")}` : "";
        $("#ptz-selall").checked = cams.length > 0 && selected.size === cams.length;
        renderBulkBar();
        // La portée d'un futur instantané suit la sélection : elle doit se mettre à jour
        // en même temps qu'elle, sinon le panneau annonce une portée périmée.
        if (view === "snapshots") $("#ptz-snap-scope").textContent = snapScope();
    }

    // Clé de tri d'une adresse IPv4 (octets → entier), Infinity si ce n'en est pas une :
    // les adresses malformées finissent en bas plutôt que de casser le tri.
    function ipKey(h) {
        const p = String(h || "").split(".");
        if (p.length !== 4) return Infinity;
        return p.reduce((a, x) => a * 256 + (parseInt(x, 10) || 0), 0);
    }

    // Un seul état de tri partagé par toutes les vues (parc, vue d'ensemble, noms) : changer
    // le tri dans l'une le reflète partout, et les trois sélecteurs restent synchronisés.
    function setFleetSort(v) {
        fleetSort = v;
        ["#ptz-sort", "#ptz-ov-sort", "#ptz-nm-sort", "#ptz-rcp-sort"].forEach((sel) => {
            const el = $(sel); if (el) el.value = v;
        });
        if (view === "fleet") renderFleet();
        else if (view === "overview") renderOverview();
        else if (view === "names") renderNames();
        else if (view === "rcp") renderRcp();
    }

    // « Utilisée » = a un numéro. Une caméra sans numéro n'est pas prise sur la presta : elle
    // est masquée des vues d'exploitation (mais reste dans le Parc et dans Ember+, où on peut
    // justement la numéroter).
    const isNumbered = (c) => c.cam_number != null;

    // Le tri ne réordonne QUE l'affichage (copie) — jamais le parc stocké. Une caméra sans
    // numéro passe après celles qui en ont un (Infinity), pour ne pas s'intercaler au milieu.
    function sortedCams() {
        const list = cams.slice();
        if (fleetSort === "number") {
            list.sort((a, b) => (a.cam_number == null ? Infinity : a.cam_number)
                              - (b.cam_number == null ? Infinity : b.cam_number));
        } else if (fleetSort === "ip") {
            list.sort((a, b) => ipKey(a.host) - ipKey(b.host));
        }
        return list;
    }

    // Portée d'un instantané : caméras de la presta (numérotées) ou tout le parc, selon le
    // sélecteur de la vue Sauvegardes (défaut : la presta).
    const snapScope = () => {
        const s = (($("#ptz-snap-scopesel") || {}).value) || "presta";
        if (s === "all") return tr("plugin.ptz.snap.all", "tout le parc sera relevé");
        const n = cams.filter(isNumbered).length;
        return `${n} ${tr("plugin.ptz.snap.presta", "caméra(s) de la presta seront relevées")}`;
    };

    function cardHtml(c) {
        // Trois états distincts, jamais confondus : joignable, injoignable, PAS ENCORE SONDÉ.
        const dot = c.reachable === true ? "ok" : c.reachable === false ? "err" : "";
        let sub = c.reachable === true
            ? (c.latency_ms != null ? c.latency_ms + " ms" : tr("plugin.ptz.ok", "joignable"))
            : c.reachable === false
                ? esc(c.error || tr("plugin.ptz.unreachable", "injoignable"))
                : tr("plugin.ptz.unknown", "non sondée");
        const model = (c.identity && c.identity.model) || c.model || "";
        return `<div class="ptz-card ${c.id === selId ? "sel" : ""} ${c.enabled ? "" : "off"}" data-id="${esc(c.id)}">
            <div class="ptz-card-h">
              <input type="checkbox" class="ptz-pick" ${selected.has(c.id) ? "checked" : ""}
                     title="${esc(tr("plugin.ptz.pick1", "Sélectionner pour une action groupée"))}">
              <span class="ptz-dot ${dot}"></span>
              ${c.cam_number != null ? `<span class="ptz-num" title="${esc(tr("plugin.ptz.f.camNumber", "Numéro de caméra"))}">N°${esc(c.cam_number)}</span>` : ""}
              <strong>${esc(c.name)}</strong>
              <span class="ptz-actions">
                <button class="btn btn-icon" data-a="edit" type="button" title="Modifier">✎</button>
                <button class="btn btn-icon btn-red" data-a="del" type="button" title="Supprimer">✕</button>
              </span>
            </div>
            <div class="ptz-card-sub">
              <span class="ptz-ip">${esc(c.host)}</span>
              ${model ? `<span class="ptz-tag">${esc(model)}</span>` : ""}
              ${c.group ? `<span class="ptz-tag">${esc(c.group)}</span>` : ""}
              <span>${sub}</span>
            </div>
          </div>`;
    }

    function cardAction(action, id) {
        if (action === "edit") return showForm(cam(id));
        if (action === "del") return removeCam(id);
    }

    function onSelectAll(e) {
        selected = e.target.checked ? new Set(cams.map((c) => c.id)) : new Set();
        renderFleet();
    }

    // ── Formulaire ───────────────────────────────────────────
    function driverOf(kind) { return catalog.find((d) => d.kind === kind); }

    function showForm(c) {
        const dsel = $("#ptz-f-driver");
        dsel.innerHTML = catalog.map((d) =>
            `<option value="${esc(d.kind)}">${esc(d.label)}${d.available ? "" : " — " + tr("plugin.ptz.unvalidated", "non validé")}</option>`
        ).join("");
        dsel.value = c ? c.driver : (catalog[0] && catalog[0].kind) || "";
        $("#ptz-groups").innerHTML = groups.map((g) => `<option value="${esc(g)}">`).join("");
        $("#ptz-form").dataset.id = c ? c.id : "";
        $("#ptz-f-name").value = c ? c.name : "";
        $("#ptz-f-host").value = c ? c.host : "";
        $("#ptz-f-port").value = c && c.port ? c.port : "";
        $("#ptz-f-group").value = c ? c.group : "";
        $("#ptz-f-camnum").value = c && c.cam_number != null ? c.cam_number : "";
        $("#ptz-f-user").value = c ? c.user : "";
        $("#ptz-f-pass").value = "";
        onDriverChange();
        if (c) $("#ptz-f-model").value = c.model || "";
        $("#ptz-form").hidden = false;
        $("#ptz-f-name").focus();
    }

    function onDriverChange() {
        const d = driverOf($("#ptz-f-driver").value);
        $("#ptz-f-model").innerHTML = ((d && d.models) || []).map((m) =>
            `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join("");
        $("#ptz-f-port").placeholder = (d && d.default_port) || "";
        $("#ptz-form-note").textContent = d && !d.available ? d.note : "";
    }

    function hideForm() { $("#ptz-form").hidden = true; $("#ptz-form").dataset.id = ""; }

    async function onFormSubmit(e) {
        e.preventDefault();
        const id = $("#ptz-form").dataset.id;
        const port = $("#ptz-f-port").value.trim();
        const body = {
            name: $("#ptz-f-name").value.trim(),
            driver: $("#ptz-f-driver").value,
            host: $("#ptz-f-host").value.trim(),
            port: port ? parseInt(port, 10) : null,
            model: $("#ptz-f-model").value,
            group: $("#ptz-f-group").value.trim(),
            cam_number: $("#ptz-f-camnum").value.trim(),   // vide = aucun (cf. serveur)
            user: $("#ptz-f-user").value.trim(),
            password: $("#ptz-f-pass").value,      // vide = inchangé (cf. serveur)
        };
        if (!body.host) { toast(tr("plugin.ptz.hostRequired", "Adresse IP requise"), "error"); return; }
        if (!body.name) body.name = body.host;
        try {
            if (id) await ctx.api("cameras/" + id, { method: "PUT", body });
            else await ctx.api("cameras", { body });
            toast(id ? tr("plugin.ptz.saved", "Caméra enregistrée")
                     : tr("plugin.ptz.added", "Caméra ajoutée"), "info");
            hideForm();
            delete schemaCache[id];
            invalidateViews();
            refresh();
        } catch (err) { toast(err.message, "error"); }
    }

    // ── Ajout en série ───────────────────────────────────────
    // Les caméras d'un plateau sont presque toujours sur des adresses consécutives avec les
    // mêmes identifiants. On saisit la première adresse et le nombre ; l'aperçu montre la
    // plage AVANT création, pour qu'une faute de frappe se voie tout de suite.
    function showBatch() {
        const sel = $("#ptz-b-driver");
        sel.innerHTML = catalog.map((d) =>
            `<option value="${esc(d.kind)}">${esc(d.label)}</option>`).join("");
        $("#ptz-groups").innerHTML = groups.map((g) => `<option value="${esc(g)}">`).join("");
        $("#ptz-batch").hidden = false;
        $("#ptz-b-host").focus();
        batchPreview();
    }

    function batchPreview() {
        const host = $("#ptz-b-host").value.trim();
        const n = parseInt($("#ptz-b-count").value, 10) || 0;
        const box = $("#ptz-b-preview");
        const m = host.match(/^(\d+\.\d+\.\d+)\.(\d+)$/);
        if (!m || n < 1) { box.textContent = ""; return; }
        const base = m[1], first = parseInt(m[2], 10), last = first + n - 1;
        if (last > 254) {
            box.textContent = tr("plugin.ptz.b.overflow",
                "La série dépasse .254 — réduisez le nombre ou changez l'adresse de départ.");
            return;
        }
        const pre = $("#ptz-b-prefix").value.trim();
        const no = parseInt($("#ptz-b-first").value, 10) || 1;
        box.textContent = `${base}.${first} → ${base}.${last}` +
            (pre ? ` · « ${pre} ${no} » … « ${pre} ${no + n - 1} »` : "");
    }

    async function onBatchSubmit(e) {
        e.preventDefault();
        const body = {
            host: $("#ptz-b-host").value.trim(),
            count: parseInt($("#ptz-b-count").value, 10) || 0,
            driver: $("#ptz-b-driver").value,
            name_prefix: $("#ptz-b-prefix").value.trim(),
            first_number: parseInt($("#ptz-b-first").value, 10) || 1,
            group: $("#ptz-b-group").value.trim(),
            user: $("#ptz-b-user").value.trim(),
            password: $("#ptz-b-pass").value,
        };
        if (!body.host || !body.count) {
            toast(tr("plugin.ptz.b.need", "Adresse de départ et nombre requis"), "error");
            return;
        }
        try {
            const r = await ctx.api("cameras/batch", { body });
            // Compte-rendu ligne à ligne : sur dix caméras, celles qui n'ont pas répondu
            // doivent se voir — elles sont créées mais sans identité.
            renderReport(tr("plugin.ptz.addBatch", "+ Ajouter en série"), r);
            toast(`${r.created} ${tr("plugin.ptz.b.added", "ajoutée(s)")}` +
                  (r.count_fail ? ` · ${r.count_fail} ${tr("plugin.ptz.b.noAnswer", "sans réponse")}` : ""),
                  r.count_fail ? "error" : "info");
            $("#ptz-batch").hidden = true;
            invalidateViews();
            refresh();
        } catch (err) { toast(err.message, "error"); }
    }

    async function removeCam(id) {
        const c = cam(id);
        if (!confirm(tr("plugin.ptz.confirmDel", "Supprimer la caméra ?") + `\n${c ? c.name : id}`)) return;
        try {
            await ctx.api("cameras/" + id, { method: "DELETE" });
            selected.delete(id);
            if (selId === id) { selId = null; renderDetailEmpty(); }
            toast(tr("plugin.ptz.deleted", "Supprimée"), "info");
            invalidateViews();
            refresh();
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Détail ───────────────────────────────────────────────
    function select(id) { selId = id; tab = "params"; renderFleet(); renderDetail(); }

    function renderDetailEmpty() {
        $("#ptz-detail").innerHTML =
            `<div class="ptz-meta">${esc(tr("plugin.ptz.pick", "Sélectionnez une caméra pour voir son détail."))}</div>`;
    }

    function renderDetail() {
        const c = cam(selId);
        if (!c) return renderDetailEmpty();
        const tabs = [["params", tr("plugin.ptz.tab.params", "Paramètres")],
                      ["presets", tr("plugin.ptz.tab.presets", "Mémoires")],
                      ["console", tr("plugin.ptz.tab.console", "Console")]];
        $("#ptz-detail").innerHTML = `
          <div id="ptz-dh"></div>
          <div class="ptz-tabs">
            ${tabs.map(([k, l]) => `<button class="ptz-tab ${tab === k ? "on" : ""}" data-tab="${k}" type="button">${esc(l)}</button>`).join("")}
          </div>
          <div id="ptz-tabbox"><div class="ptz-meta">${esc(tr("plugin.ptz.reading", "Lecture…"))}</div></div>`;
        renderDetailHeader();
        $("#ptz-detail").querySelectorAll(".ptz-tab").forEach((b) =>
            b.addEventListener("click", () => { tab = b.dataset.tab; renderDetail(); }));
        if (tab === "params") tabParams();
        else if (tab === "presets") tabPresets();
        else tabConsole();
    }

    function renderDetailHeader() {
        const box = root && root.querySelector("#ptz-dh");
        const c = cam(selId);
        if (!box || !c) return;
        const id = c.identity || {};
        const dot = c.reachable === true ? "ok" : c.reachable === false ? "err" : "";
        const bits = [id.model || c.model, id.firmware ? "fw " + id.firmware : "",
                      id.serial ? "s/n " + id.serial : "", id.mac].filter(Boolean);
        box.innerHTML = `
          <div class="ptz-detail-h">
            <span class="ptz-dot ${dot}"></span>
            <strong>${esc(c.name)}</strong>
            <span class="ptz-ip">${esc(c.host)}</span>
            <span class="ptz-detail-actions">
              ${can(c, "power") ? `<button class="btn" data-pw="1" type="button">${esc(tr("plugin.ptz.on", "Marche"))}</button>
              <button class="btn" data-pw="0" type="button">${esc(tr("plugin.ptz.standby", "Veille"))}</button>` : ""}
              <button class="btn" data-ident="1" type="button">${esc(tr("plugin.ptz.identify", "Détecter le modèle"))}</button>
              ${can(c, "network") ? `<button class="btn" data-netip="1" type="button" title="${esc(tr("plugin.ptz.ipTip", "Changer l'adresse IP de la caméra"))}">${esc(tr("plugin.ptz.changeIp", "Changer l'IP"))}</button>` : ""}
              <button class="btn" data-web="${esc(c.host)}" type="button" title="${esc(tr("plugin.ptz.webPageTip", "Ouvrir l'interface web dans un nouvel onglet"))}">${esc(tr("plugin.ptz.webPage", "Page web ↗"))}</button>
            </span>
          </div>
          <div class="ptz-ident">${bits.length ? esc(bits.join(" · "))
              : esc(tr("plugin.ptz.noIdent", "modèle inconnu — cliquez sur « Détecter le modèle »"))}</div>`;
        box.querySelectorAll("[data-pw]").forEach((b) =>
            b.addEventListener("click", () => setPower(b.dataset.pw === "1")));
        const ib = box.querySelector("[data-ident]");
        if (ib) ib.addEventListener("click", identify);
        const wb = box.querySelector("[data-web]");
        if (wb) wb.addEventListener("click", () => openWebPage(wb.dataset.web));
        const nb = box.querySelector("[data-netip]");
        if (nb) nb.addEventListener("click", changeIp);
    }

    // Change l'IP de la caméra sélectionnée : lit la config courante, demande la nouvelle
    // adresse, confirme (opération à risque : la caméra bascule d'adresse), puis met à jour
    // le parc. Le serveur bloque si l'IP est déjà prise par une caméra de la presta.
    async function changeIp() {
        if (!selId) return;
        let net;
        try { net = await ctx.api("cameras/" + selId + "/network"); }
        catch (e) { toast(e.message, "error"); return; }
        const cur = net.ip4_addr || "";
        const ip = (window.prompt(tr("plugin.ptz.ipPrompt",
            "Nouvelle adresse IP de la caméra :"), cur) || "").trim();
        if (!ip || ip === cur) return;
        if (!window.confirm(tr("plugin.ptz.ipConfirm",
            "Changer l'adresse ? La caméra va basculer sur la nouvelle IP (elle peut redémarrer, "
            + "et la connexion à l'ancienne adresse sera perdue).") + `\n${cur} → ${ip}`)) return;
        try {
            const r = await ctx.api("cameras/" + selId + "/network", { body: { ip } });
            if (r.warning) toast("⚠ " + r.warning, "info");
            toast(tr("plugin.ptz.ipChanged", "Adresse changée") + ` → ${ip}`, "success");
            invalidateViews(); await refresh();
        } catch (e) { toast(e.message, "error"); }
    }

    async function rediscover() {
        const b = $("#ptz-discover");
        b.disabled = true;
        toast(tr("plugin.ptz.discovering", "Balayage du réseau…"), "info");
        try {
            const r = await ctx.api("discover", { body: {} });
            const n = (r.changes || []).length;
            if (n) {
                r.changes.forEach((c) => toast(`${c.name} : ${c.old} → ${c.new}`, "success"));
                invalidateViews(); await refresh();
            } else {
                toast(tr("plugin.ptz.discoverNone", "Aucune caméra à re-localiser")
                    + ` (${r.found} ${tr("plugin.ptz.found", "trouvées")})`, "info");
            }
        } catch (e) { toast(e.message, "error"); }
        finally { b.disabled = false; }
    }

    // Ouvre l'interface web d'un équipement dans un nouvel onglet du navigateur de
    // l'opérateur (pas du conteneur : c'est le poste de l'utilisateur qui joint la caméra).
    function openWebPage(host) {
        if (!host) return;
        window.open("http://" + host + "/", "_blank", "noopener");
    }

    async function setPower(on) {
        try {
            await ctx.api("cameras/" + selId + "/power", { body: { on } });
            toast(on ? tr("plugin.ptz.poweredOn", "Réveil en cours… (jusqu'à ~30 s selon le modèle)")
                     : tr("plugin.ptz.poweredOff", "Caméra en veille"), "info");
        } catch (e) { toast(e.message, "error"); }
    }

    async function identify() {
        try {
            await ctx.api("cameras/" + selId + "/identify", { body: {} });
            toast(tr("plugin.ptz.identified", "Modèle détecté"), "info");
            delete schemaCache[selId];
            await refresh();
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Onglet Paramètres ────────────────────────────────────
    async function tabParams() {
        const box = $("#ptz-tabbox");
        let data;
        try { data = await ctx.api("cameras/" + selId + "/params"); }
        catch (e) { box.innerHTML = `<div class="ptz-meta">${esc(e.message)}</div>`; return; }
        const schema = data.schema || [];
        schemaCache[selId] = schema;
        const values = data.values || {};
        if (!schema.length) {
            box.innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.noParams",
                "Ce pilote n'expose pas encore de paramètres."))}</div>`;
            return;
        }
        // Les paramètres rattachés à une sortie vidéo (champ `output`) sortent de la liste
        // pour former un tableau : une ligne par sortie. Une caméra qui n'a qu'un réglage
        // général donne un tableau d'une ligne — l'UI ne teste jamais le modèle.
        const outParams = schema.filter((p) => p.output !== null && p.output !== undefined);
        const plain = schema.filter((p) => p.output === null || p.output === undefined);
        const byGroup = {};
        plain.forEach((p) => (byGroup[p.group] = byGroup[p.group] || []).push(p));

        box.innerHTML = `
          ${outParams.length ? outputsTable(data.outputs || [], outParams, values) : ""}
          <div class="ptz-params">${Object.keys(byGroup).map((g) => `
            <div>
              <div class="ptz-group-h">${esc(g)}</div>
              ${byGroup[g].map((p) => paramRow(p, values[p.key])).join("")}
            </div>`).join("")}</div>`;
        box.querySelectorAll("[data-pkey]").forEach((el) =>
            el.addEventListener("change", () => writeParam(el)));
    }

    // Tableau des sorties vidéo : lignes = sorties (le réglage général en premier),
    // colonnes = réglages déclarés pour au moins une sortie.
    function outputsTable(outputs, params, values) {
        const rows = outputs.length ? outputs : [{ value: "", label: "Réglage général" }];
        // Colonnes = clés distinctes, dans l'ordre du schéma. Une même colonne peut
        // n'exister que pour certaines sorties : la cellule est alors vide, pas fautive.
        const cols = [];
        params.forEach((p) => { if (!cols.some((c) => c.label === p.label)) cols.push(p); });
        const cell = (out, col) => {
            const p = params.find((x) => x.output === out.value && x.label === col.label);
            if (!p) return `<td class="ptz-out-na">—</td>`;
            return `<td>${paramControl(p, values[p.key])}</td>`;
        };
        return `<div class="ptz-outputs">
            <div class="ptz-group-h">${esc(tr("plugin.ptz.outputs", "Sorties vidéo"))}</div>
            <table class="ptz-tbl ptz-out-tbl">
              <thead><tr><th>${esc(tr("plugin.ptz.output", "Sortie"))}</th>
                ${cols.map((c) => `<th>${esc(c.label)}${c.validated ? "" : " ⚠"}</th>`).join("")}</tr></thead>
              <tbody>${rows.map((o) => `<tr>
                  <td class="ptz-out-name">${esc(o.label)}</td>
                  ${cols.map((c) => cell(o, c)).join("")}
                </tr>`).join("")}</tbody>
            </table>
          </div>`;
    }

    // Contrôle d'un paramètre, construit d'après son TYPE déclaré. Partagé par la liste et
    // par le tableau des sorties : un même paramètre s'édite pareil où qu'il s'affiche.
    function paramControl(p, value) {
        // `undefined`/`null` = NON LU. On l'affiche tel quel : confondre « non lu » avec
        // une valeur par défaut ferait croire à un réglage qu'on n'a jamais constaté.
        const unread = value === null || value === undefined;
        let ctl;
        if (!p.writable) {
            ctl = unread ? nullSpan() : `<span>${esc(value)}</span>`;
        } else if (p.type === "bool") {
            ctl = `<input type="checkbox" class="ios-toggle" data-pkey="${esc(p.key)}" data-ptype="bool"
                     ${value === true ? "checked" : ""} ${unread ? "" : ""}>`;
        } else if (p.type === "enum") {
            ctl = `<select data-pkey="${esc(p.key)}" data-ptype="enum">
                     <option value="">${esc(unread ? tr("plugin.ptz.notRead", "non lu") : "—")}</option>
                     ${(p.options || []).map((o) =>
                        `<option value="${esc(o.value)}" ${o.value === value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}
                   </select>`;
        } else if (p.type === "int") {
            ctl = `<input type="number" data-pkey="${esc(p.key)}" data-ptype="int"
                     ${p.min != null ? `min="${p.min}"` : ""} ${p.max != null ? `max="${p.max}"` : ""}
                     value="${unread ? "" : esc(value)}" placeholder="${esc(tr("plugin.ptz.notRead", "non lu"))}">`;
        } else {
            ctl = `<input type="text" data-pkey="${esc(p.key)}" data-ptype="text"
                     value="${unread ? "" : esc(value)}" placeholder="${esc(tr("plugin.ptz.notRead", "non lu"))}">`;
        }
        return ctl;
    }

    function paramRow(p, value) {
        const warn = p.validated ? "" :
            `<span class="ptz-unval" title="${esc(tr("plugin.ptz.unvalidatedHelp",
                "Commande non confirmée sur ce modèle — à vérifier dans l'onglet Console."))}">⚠</span>`;
        return `<div class="ptz-prow">
            <div class="ptz-plabel">
              <span>${esc(p.label)}${warn}${p.help ? `<span class="ptz-phelp">${esc(p.help)}</span>` : ""}</span>
            </div>
            <div class="ptz-pval">${paramControl(p, value)}</div>
          </div>`;
    }

    const nullSpan = () => `<span class="ptz-null">${tr("plugin.ptz.notRead", "non lu")}</span>`;

    async function writeParam(el) {
        const key = el.dataset.pkey;
        let val = el.dataset.ptype === "bool" ? el.checked
                : el.dataset.ptype === "int" ? parseInt(el.value, 10) : el.value;
        if (el.dataset.ptype === "enum" && !val) return;      // « — » n'écrit rien
        try {
            const r = await ctx.api("cameras/" + selId + "/params", { body: { values: { [key]: val } } });
            const rep = (r.report || {})[key] || {};
            if (rep.ok) toast(tr("plugin.ptz.written", "Paramètre appliqué"), "info");
            else toast(rep.error || tr("plugin.ptz.writeFail", "Écriture refusée"), "error");
            // Un réglage peut en contraindre d'autres (sur Sony, le Rec Format filtre les
            // options de codec/scan et la liste des sorties SDI/HDMI). On relit le schéma pour
            // recalculer le masquage et les valeurs. delete du cache : on veut du frais.
            delete schemaCache[selId];
            await tabParams();
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Onglet Mémoires ──────────────────────────────────────
    async function tabPresets() {
        const box = $("#ptz-tabbox");
        let data;
        try { data = await ctx.api("cameras/" + selId + "/presets"); }
        catch (e) { box.innerHTML = `<div class="ptz-meta">${esc(e.message)}</div>`; return; }
        const rows = data.presets || [];
        const named = rows.filter((p) => p.name).length;
        box.innerHTML = `
          <div class="ptz-presets-bar">
            <span class="ptz-meta">${named} / ${rows.length} ${esc(tr("plugin.ptz.named", "nommées"))}</span>
            <span class="ptz-spacer"></span>
            <label class="ptz-selall"><input type="checkbox" id="ptz-only-named">
              <span>${esc(tr("plugin.ptz.onlyNamed", "N'afficher que les nommées"))}</span></label>
          </div>
          <div class="ptz-presets" id="ptz-preset-grid"></div>
          <p class="ptz-meta" style="margin-top:10px">${esc(tr("plugin.ptz.presetHelp",
            "Les noms sont tenus par l'outil (le protocole des caméras n'en transporte pas) : " +
            "ils valent pour tout le parc, quel que soit le modèle. ▶ rappelle la mémoire, " +
            "● l'enregistre sur la position courante."))}</p>`;
        const grid = $("#ptz-preset-grid");
        const draw = (onlyNamed) => {
            const list = onlyNamed ? rows.filter((p) => p.name) : rows;
            grid.innerHTML = list.length ? list.map(presetHtml).join("")
                : `<div class="ptz-empty">${esc(tr("plugin.ptz.noNamed", "Aucune mémoire nommée."))}</div>`;
            grid.querySelectorAll(".ptz-pname").forEach((i) =>
                i.addEventListener("change", () => renamePreset(i.dataset.idx, i.value)));
            grid.querySelectorAll("[data-pa]").forEach((b) =>
                b.addEventListener("click", () => presetAction(b.dataset.pa, b.dataset.idx)));
        };
        draw(false);
        $("#ptz-only-named").addEventListener("change", (e) => draw(e.target.checked));
    }

    function presetHtml(p) {
        return `<div class="ptz-preset ${p.name ? "named" : ""}">
            <span class="ptz-pnum">${p.index}</span>
            <input class="ptz-pname" data-idx="${p.index}" value="${esc(p.name)}"
                   placeholder="${esc(tr("plugin.ptz.unnamed", "sans nom"))}" maxlength="64">
            <button class="btn btn-icon" data-pa="recall" data-idx="${p.index}" type="button"
                    title="${esc(tr("plugin.ptz.recall", "Rappeler cette mémoire"))}">▶</button>
            <button class="btn btn-icon" data-pa="store" data-idx="${p.index}" type="button"
                    title="${esc(tr("plugin.ptz.store", "Enregistrer la position courante ici"))}">●</button>
          </div>`;
    }

    async function presetAction(action, index) {
        if (action === "store" && !confirm(
            tr("plugin.ptz.confirmStore", "Écraser la mémoire ?") + " " + index)) return;
        try {
            await ctx.api("cameras/" + selId + "/presets/" + action, { body: { index: parseInt(index, 10) } });
            toast(action === "store" ? tr("plugin.ptz.stored", "Mémoire enregistrée")
                                     : tr("plugin.ptz.recalled", "Mémoire rappelée"), "info");
        } catch (e) { toast(e.message, "error"); }
    }

    async function renamePreset(index, name) {
        try {
            await ctx.api("cameras/" + selId + "/presets/name",
                { body: { index: parseInt(index, 10), name } });
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Onglet Console ───────────────────────────────────────
    // Envoi de commandes brutes : c'est l'outil qui permet de CONFIRMER une commande sur
    // du matériel réel avant de la câbler dans le pilote (démarche du plugin neuron).
    function tabConsole() {
        const c = cam(selId);
        $("#ptz-tabbox").innerHTML = `
          <div class="ptz-console-bar">
            <select id="ptz-chan">
              <option value="ptz">${esc(tr("plugin.ptz.chanPtz", "Tourelle (aw_ptz)"))}</option>
              <option value="cam">${esc(tr("plugin.ptz.chanCam", "Caméra (aw_cam)"))}</option>
            </select>
            <input type="text" id="ptz-cmd" placeholder="#O" spellcheck="false">
            <button class="btn btn-green" id="ptz-send" type="button">${esc(tr("plugin.ptz.send", "Envoyer"))}</button>
            <button class="btn" id="ptz-doprobe" type="button">${esc(tr("plugin.ptz.probe", "Sonder les commandes connues"))}</button>
          </div>
          <div class="ptz-log" id="ptz-log"></div>
          <p class="ptz-meta" style="margin-top:10px">${esc(tr("plugin.ptz.consoleHelp",
            "« Sonder » envoie une batterie de commandes de LECTURE seulement (aucune écriture) " +
            "et affiche les réponses brutes. C'est ce relevé qui permet de fiabiliser les " +
            "paramètres marqués ⚠."))}</p>`;
        if (!can(c, "console")) {
            $("#ptz-tabbox").innerHTML =
                `<div class="ptz-meta">${esc(tr("plugin.ptz.noConsole", "Ce pilote n'expose pas de console."))}</div>`;
            return;
        }
        $("#ptz-send").addEventListener("click", sendCmd);
        $("#ptz-cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") sendCmd(); });
        $("#ptz-doprobe").addEventListener("click", doProbe);
    }

    function logLine(cls, text) {
        const log = $("#ptz-log");
        if (!log) return;
        const d = document.createElement("div");
        d.className = cls; d.textContent = text;
        log.appendChild(d);
        log.scrollTop = log.scrollHeight;
    }

    async function sendCmd() {
        const cmd = $("#ptz-cmd").value.trim();
        if (!cmd) return;
        const channel = $("#ptz-chan").value;
        logLine("cmd", `→ [${channel}] ${cmd}`);
        try {
            const r = await ctx.api("cameras/" + selId + "/console", { body: { cmd, channel } });
            logLine(r.error ? "err" : "ok", `← ${r.response}`);
        } catch (e) { logLine("err", `← ${e.message}`); }
    }

    async function doProbe() {
        const b = $("#ptz-doprobe");
        b.disabled = true;
        logLine("cmd", "— " + tr("plugin.ptz.probing", "sondage en cours…"));
        try {
            const r = await ctx.api("cameras/" + selId + "/probe", { body: {} });
            (r.probe || []).forEach((p) =>
                logLine(p.ok ? "ok" : "err", `[${p.channel}] ${p.cmd}  ${p.comment}\n    ← ${p.response}`));
        } catch (e) { logLine("err", e.message); }
        finally { b.disabled = false; }
    }

    // ── Vues de premier niveau ───────────────────────────────
    // Une seule vue à la fois : un tableau large n'est lisible que s'il a toute la page.
    // Chargement PARESSEUX — on ne relit pas les caméras à chaque va-et-vient entre
    // onglets ; le bouton ↻ de chaque vue force la relecture.
    function setView(name) {
        view = name;
        root.querySelectorAll(".ptz-view").forEach((el) => {
            el.hidden = el.id !== "ptz-view-" + name;
        });
        root.querySelectorAll(".ptz-view-tab").forEach((b) =>
            b.classList.toggle("on", b.dataset.view === name));
        const load = { overview: loadOverview, names: loadNames, snapshots: loadSnaps,
                       panels: loadPanels, grid: loadGrid, presta: loadPresta,
                       shotbox: loadShotbox, rcp: loadRcp }[name];
        if (load && !viewLoaded[name]) { viewLoaded[name] = true; load(); }
        if (name === "snapshots") $("#ptz-snap-scope").textContent = snapScope();
    }

    // ── Vue d'ensemble ───────────────────────────────────────
    // Chargement PROGRESSIF : le squelette (lignes, colonnes) se dessine tout de suite à
    // partir de ce que le pilote déclare sans toucher aux caméras, puis chaque caméra
    // remplit sa ligne quand elle répond. Une caméra lente ou morte ne retient plus les
    // autres : elle affiche son motif d'erreur à sa place, quand il arrive.
    let ovData = {};                   // { camId: {outputs, values} }
    let ovErr = {};                    // { camId: message }

    function ovColumns() {
        const cols = [], seen = new Set();
        cams.forEach((c) => (c.outputs || []).forEach((o) => {
            if (!seen.has(o.value)) { seen.add(o.value); cols.push(o); }
        }));
        return cols;
    }

    function loadOverview() {
        ovData = {}; ovErr = {};
        renderOverview();
        cams.forEach((c) => {
            ctx.api("cameras/" + c.id + "/overview")
                .then((d) => { ovData[c.id] = d; fillOverviewRow(c.id); })
                .catch((e) => { ovErr[c.id] = e.message; fillOverviewRow(c.id); });
        });
    }

    function renderOverview() {
        const body = $("#ptz-ov-body");
        const cols = ovColumns();
        if (!cams.length) {
            body.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.none",
                "Aucune caméra — utilisez « + Ajouter une caméra »."))}</div>`;
            return;
        }
        body.innerHTML = `<div class="ptz-ov-scroll"><table class="ptz-tbl ptz-ov-tbl">
            <thead><tr>
              <th>${esc(tr("plugin.ptz.pn.slot", "N°"))}</th>
              <th>${esc(tr("plugin.ptz.camera", "Caméra"))}</th>
              <th>${esc(tr("plugin.ptz.f.model", "Modèle"))}</th>
              <th>${esc(tr("plugin.ptz.freq", "Fréquence"))}</th>
              ${cols.map((c) => `<th>${esc(c.label)}</th>`).join("")}
            </tr></thead>
            <tbody>${sortedCams().filter(isNumbered).map((c) => `<tr data-row-for="${esc(c.id)}">
                <td class="ptz-pn-slot">${c.cam_number != null ? esc(c.cam_number) : ""}</td>
                <td><span class="ptz-dot" data-dot-for="${esc(c.id)}"></span>
                    <a class="ptz-ov-link" data-cam="${esc(c.id)}">${esc(c.name)}</a></td>
                <td class="ptz-meta">${esc((c.identity && c.identity.model) || c.model || "")}</td>
                <td class="ptz-ov-cell" data-cell="freq">…</td>
                ${cols.map((o) => (c.outputs || []).some((x) => x.value === o.value)
                    ? `<td class="ptz-ov-cell" data-cell="${esc(o.value)}">…</td>`
                    : `<td class="ptz-out-na"></td>`).join("")}
              </tr>`).join("")}</tbody>
          </table></div>`;
        body.querySelectorAll("[data-cam]").forEach((el) =>
            el.addEventListener("click", () => { setView("fleet"); select(el.dataset.cam); }));
        applyStatusDots();
        Object.keys(ovData).forEach(fillOverviewRow);
        Object.keys(ovErr).forEach(fillOverviewRow);
        updateOvMeta();
    }

    function fillOverviewRow(id) {
        const tr_ = root && root.querySelector(`[data-row-for="${id}"]`);
        if (!tr_) return;
        const err = ovErr[id], d = ovData[id];
        const cells = tr_.querySelectorAll(".ptz-ov-cell");
        if (err) {
            cells.forEach((td, i) => {
                td.textContent = i === 0 ? "⚠" : "";
                td.className = "ptz-ov-cell fail";
                td.title = err;
            });
            tr_.classList.add("ptz-row-down");
            updateOvMeta();
            return;
        }
        if (!d) return;
        tr_.classList.remove("ptz-row-down");
        cells.forEach((td) => {
            const key = td.dataset.cell;
            const v = key === "freq" ? d.freq_label
                                     : d.values["video_format" + (key ? "_" + key : "")];
            td.className = "ptz-ov-cell";
            td.title = "";
            if (v === null || v === undefined) {
                td.innerHTML = `<span class="ptz-null">${esc(tr("plugin.ptz.notRead", "non lu"))}</span>`;
            } else {
                td.textContent = v;
            }
        });
        updateOvMeta();
    }

    function updateOvMeta() {
        const ok = Object.keys(ovData).length, ko = Object.keys(ovErr).length;
        const el = $("#ptz-ov-meta");
        if (!el) return;
        el.textContent = (ok + ko < cams.length)
            ? `${ok + ko}/${cams.length} ${tr("plugin.ptz.read", "lue(s)")}…`
            : `${ok} ${tr("plugin.ptz.read", "lue(s)")}` +
              (ko ? ` · ${ko} ${tr("plugin.ptz.failed", "échec(s)")}` : "");
    }

    // ── Vue RCP (pupitre colorimétrie) ───────────────────────
    // Vrai pupitre RCP : caméras en COLONNES, réglages colorimétriques en LIGNES groupées.
    // L'onglet ne code AUCUNE marque ni aucun réglage en dur : il garde tout paramètre dont
    // le pilote déclare `color === true`. Les caméras de marques différentes n'ayant pas les
    // mêmes réglages, on fait l'UNION par (group, triplet|key) ; une caméra qui n'a pas un
    // réglage montre « — ». Deux ergonomies au choix (rotatifs / boutons ±), deux vues
    // (complète / compacte avec ★), R/V/B côte à côte, référence + décalage par cellule.
    let rcpData = {};                  // { camId: { byKey:{key:param}, values:{key:val} } }
    let rcpErr = {};                   // { camId: message }
    let rcpRef = null;                 // { camId: { key: value } } | null
    let rcpRefDate = null;             // date ISO de la mémorisation | null
    let rcpStyle = "knob";             // ergonomie des mini-contrôles int de l'ÉCRAN : knob | step
    let rcpDiaphStyle = "fader";       // ergonomie du Diaph : fader | knob
    let rcpPedStyle = "wheel";         // ergonomie du Pedestal : wheel | knob
    let rcpCat = "fav";                // catégorie affichée dans l'ÉCRAN : "fav" ou un nom de groupe
    let rcpViewMode = "full";          // vue : full (tout) | compact (diaph + pedestal seuls)
    let rcpValueMode = "delta";        // affichage : delta (valeur+écart, pilotable) | ref (valeur de référence, lecture)
    let rcpPins = new Set();           // FAVORIS : clés de paramètres épinglés (★), toutes caméras
    const rcpTimers = {};              // débounce d'écriture par "camId|key"
    let rcpDrag = null;                // rotatif en cours de glissement { camId, key, startY, startVal }
    let rcpDragActive = false;         // un glissement est en cours (gèle le re-rendu global)
    let rcpRenderPending = false;      // un re-rendu a été demandé pendant un glissement

    // Persistance de la référence. On PRÉFÈRE ctx.store (niveau app, partagé entre
    // utilisateurs) ; mais l'outil ptz n'ouvre pas le store générique (`store_api`), donc
    // ctx.store répond 403 : on se replie alors sur localStorage, propre au poste. Le premier
    // accès détermine lequel est utilisable et on s'y tient ensuite.
    const RCP_SCOPE = "rcp";
    const RCP_REF_NAME = "color_reference";
    const RCP_PINS_NAME = "pinned_rows";
    const RCP_LS_REF = "ptz_color_reference";
    const RCP_LS_PINS = "ptz_rcp_pins";
    const RCP_LS_STYLE = "ptz_rcp_style";
    const RCP_LS_DIAPH = "ptz_rcp_diaph";
    const RCP_LS_PED = "ptz_rcp_ped";
    const RCP_LS_CAT = "ptz_rcp_cat";
    const RCP_LS_VIEW = "ptz_rcp_view";
    const RCP_LS_CMP = "ptz_rcp_valmode";
    let rcpStoreOk = null;             // null=inconnu · true=ctx.store · false=localStorage
    let rcpRefId = null;               // id de l'entrée store « référence » (si ctx.store)
    let rcpPinsId = null;              // id de l'entrée store « épingles » (si ctx.store)

    // Un seul list() pour la référence ET les épingles (même scope). Détermine du même coup
    // si le store applicatif répond (store_api) ; sinon on se rabat sur localStorage.
    async function rcpStoreItems() {
        if (rcpStoreOk === false || !ctx.store) return null;
        try { const items = await ctx.store.list(RCP_SCOPE); rcpStoreOk = true; return items || []; }
        catch (e) { rcpStoreOk = false; return null; }
    }

    async function rcpPersist(name, idRef, payload) {
        if (rcpStoreOk !== false && ctx.store) {
            try {
                if (idRef.id) await ctx.store.update(idRef.id, { value: payload });
                else { const r = await ctx.store.create(name, payload, { scope: RCP_SCOPE, unique: true }); idRef.id = r && r.id; }
                rcpStoreOk = true; return true;
            } catch (e) { rcpStoreOk = false; }
        }
        return false;
    }

    async function rcpRefPersist(payload) {
        const ref = { id: rcpRefId };
        const ok = await rcpPersist(RCP_REF_NAME, ref, payload);
        rcpRefId = ref.id;
        if (!ok) { try { localStorage.setItem(RCP_LS_REF, JSON.stringify(payload)); } catch (e) { /* quota */ } }
    }

    async function rcpRefRemove() {
        if (rcpStoreOk && rcpRefId) { try { await ctx.store.remove(rcpRefId); } catch (e) { /* ignore */ } }
        rcpRefId = null;
        try { localStorage.removeItem(RCP_LS_REF); } catch (e) { /* ignore */ }
    }

    async function rcpPinsPersist() {
        const payload = [...rcpPins];
        const ref = { id: rcpPinsId };
        const ok = await rcpPersist(RCP_PINS_NAME, ref, payload);
        rcpPinsId = ref.id;
        if (!ok) { try { localStorage.setItem(RCP_LS_PINS, JSON.stringify(payload)); } catch (e) { /* quota */ } }
    }

    function rcpLsGet(key) {
        try { const s = localStorage.getItem(key); return s ? JSON.parse(s) : null; } catch (e) { return null; }
    }

    // Ne garde que les paramètres colorimétriques (`color === true`), dans l'ordre du schéma.
    function rcpColorOf(d) {
        const schema = (d && d.schema) || [];
        const values = (d && d.values) || {};
        const byKey = {};
        schema.filter((p) => p.color === true).forEach((p) => { byKey[p.key] = p; });
        return { byKey, values };
    }

    const rcpCams = () => sortedCams().filter(isNumbered);

    async function loadRcp() {
        rcpData = {}; rcpErr = {};
        // Préférences perso (ergonomie + vue) : poste local.
        rcpStyle = "knob";                 // mini-contrôles de l'écran : rotatifs (plus de bascule ±)
        rcpDiaphStyle = (localStorage.getItem(RCP_LS_DIAPH) === "knob") ? "knob" : "fader";
        rcpPedStyle = (localStorage.getItem(RCP_LS_PED) === "knob") ? "knob" : "wheel";
        rcpCat = localStorage.getItem(RCP_LS_CAT) || "fav";
        rcpViewMode = (localStorage.getItem(RCP_LS_VIEW) === "compact") ? "compact" : "full";
        rcpValueMode = (localStorage.getItem(RCP_LS_CMP) === "ref") ? "ref" : "delta";
        // Référence + épingles : partagées via ctx.store si dispo, sinon localStorage.
        const items = await rcpStoreItems();
        let refPayload, pinsPayload;
        if (items) {
            const r = items.find((x) => x.name === RCP_REF_NAME);
            rcpRefId = r ? r.id : null; refPayload = r ? r.value : null;
            const p = items.find((x) => x.name === RCP_PINS_NAME);
            rcpPinsId = p ? p.id : null; pinsPayload = p ? p.value : null;
        } else {
            refPayload = rcpLsGet(RCP_LS_REF); pinsPayload = rcpLsGet(RCP_LS_PINS);
        }
        rcpRef = refPayload && refPayload.ref ? refPayload.ref : null;
        rcpRefDate = refPayload && refPayload.date ? refPayload.date : null;
        rcpPins = new Set(Array.isArray(pinsPayload) ? pinsPayload : []);
        renderRcp();
        // Remplissage progressif : une caméra lente ou morte ne retient pas les autres.
        rcpCams().forEach((c) => {
            ctx.api("cameras/" + c.id + "/params")
                .then((d) => { rcpData[c.id] = rcpColorOf(d); delete rcpErr[c.id]; renderRcp(); })
                .catch((e) => { rcpErr[c.id] = e.message; renderRcp(); });
        });
    }

    function rcpSetStyle(s) {
        if (s !== "knob" && s !== "step") return;
        rcpStyle = s;
        try { localStorage.setItem(RCP_LS_STYLE, s); } catch (e) { /* ignore */ }
        renderRcp();
    }

    // Ergonomie du Diaph (fader vertical / gros rotatif) et du Pedestal (molette / rotatif) :
    // préférence PERSO du poste (localStorage), comme la vue.
    function rcpSetDiaphStyle(s) {
        if (s !== "fader" && s !== "knob") return;
        rcpDiaphStyle = s;
        try { localStorage.setItem(RCP_LS_DIAPH, s); } catch (e) { /* ignore */ }
        renderRcp();
    }
    function rcpSetPedStyle(s) {
        if (s !== "wheel" && s !== "knob") return;
        rcpPedStyle = s;
        try { localStorage.setItem(RCP_LS_PED, s); } catch (e) { /* ignore */ }
        renderRcp();
    }
    // Catégorie de l'ÉCRAN LCD (onglet courant) : "fav" ou un nom de groupe.
    function rcpSetCat(cat) {
        if (!cat) return;
        rcpCat = cat;
        try { localStorage.setItem(RCP_LS_CAT, cat); } catch (e) { /* ignore */ }
        renderRcp();
    }

    function rcpSetViewMode(v) {
        if (v !== "full" && v !== "compact") return;
        rcpViewMode = v;
        try { localStorage.setItem(RCP_LS_VIEW, v); } catch (e) { /* ignore */ }
        renderRcp();
    }

    // « Écart » (defaut) : chaque cellule montre la valeur COURANTE + le décalage vs référence,
    // et pilote la caméra. « Référence » : chaque cellule affiche la valeur de RÉFÉRENCE
    // mémorisée (la cible) et passe en lecture seule (aucune écriture au survol/drag/clic).
    function rcpSetValueMode(m) {
        if (m !== "delta" && m !== "ref") return;
        rcpValueMode = m;
        try { localStorage.setItem(RCP_LS_CMP, m); } catch (e) { /* ignore */ }
        renderRcp();
    }

    // Épingle/désépingle un réglage en FAVORI (par clé de paramètre). La liste est partagée
    // (ctx.store) avec repli localStorage — mécanisme repris des anciennes « épingles ».
    function rcpToggleFav(key) {
        if (!key) return;
        if (rcpPins.has(key)) rcpPins.delete(key); else rcpPins.add(key);
        rcpPinsPersist();
        renderRcp();
    }

    function syncRcpToggles() {
        const dp = $("#ptz-rcp-diaph");
        if (dp) dp.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.d === rcpDiaphStyle));
        const pd = $("#ptz-rcp-ped");
        if (pd) pd.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.p === rcpPedStyle));
        const vw = $("#ptz-rcp-view");
        if (vw) vw.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === rcpViewMode));
        const cm = $("#ptz-rcp-cmp");
        if (cm) cm.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.m === rcpValueMode));
    }

    // Décalage d'une cellule vs référence de CETTE caméra pour CE réglage. `int` → delta
    // signé ; enum/bool/text → « modifié » si différent. Rien si aucune référence pour ce
    // couple (caméra, réglage).
    function rcpRefVal(camId, key) {
        return rcpRef && rcpRef[camId] ? rcpRef[camId][key] : undefined;
    }

    // Valeur à AFFICHER dans une cellule selon la bascule : en mode « Référence » c'est la
    // valeur mémorisée (la cible ; null si aucune), sinon la valeur courante lue sur la caméra.
    function rcpCellVal(camId, key) {
        if (rcpValueMode === "ref") { const v = rcpRefVal(camId, key); return v === undefined ? null : v; }
        const dd = rcpData[camId];
        return dd ? dd.values[key] : null;
    }
    // Les cellules sont-elles en lecture seule ? (mode « Référence » = on regarde la cible.)
    const rcpReadonly = () => rcpValueMode === "ref";

    // Canaux R/V/B : lettre affichée + couleur. `G` (green) et `V` (vert) sont synonymes.
    const RCP_CH = {
        R: { L: "R", c: "var(--rcp-cR)" },
        G: { L: "V", c: "var(--rcp-cV)" },
        V: { L: "V", c: "var(--rcp-cV)" },
        B: { L: "B", c: "var(--rcp-cB)" },
    };
    const rcpChanOrder = (ch) => ch === "R" ? 0 : (ch === "G" || ch === "V") ? 1 : ch === "B" ? 2 : 3;

    const rcpNum = (v) => (v === null || v === undefined || v === "") ? NaN : Number(v);

    function rcpClamp(p, v) {
        const s = (p.step && p.step > 0) ? p.step : 1;
        let x = Math.round(v / s) * s;
        x = Math.round(x * 1e6) / 1e6;                 // évite les artefacts flottants
        if (p.min != null) x = Math.max(p.min, x);
        if (p.max != null) x = Math.min(p.max, x);
        return x;
    }
    // Pas d'un cran : petit pas (`step`) ou grand pas (`big`, à défaut 10×step) si Alt/Maj.
    function rcpStepVal(p, cur, dir, big) {
        const step = big ? (p.big || (p.step || 1) * 10) : (p.step || 1);
        const base = isNaN(rcpNum(cur)) ? (p.min != null ? p.min : 0) : rcpNum(cur);
        return rcpClamp(p, base + step * dir);
    }

    // Géométrie du rotatif (arc -135°→+135°), en coordonnées du viewBox 74×70.
    const RCP_KA0 = -135, RCP_KA1 = 135, RCP_KR = 27, RCP_KCX = 37, RCP_KCY = 34;
    function rcpKnobAng(v, min, max) {
        const lo = (min == null ? 0 : min), hi = (max == null ? 100 : max);
        let t = (hi > lo) ? (rcpNum(v) - lo) / (hi - lo) : 0;
        if (isNaN(t)) t = 0;
        t = Math.max(0, Math.min(1, t));
        return RCP_KA0 + (RCP_KA1 - RCP_KA0) * t;
    }
    const rcpPolar = (deg, r) => {
        const rad = (deg - 90) * Math.PI / 180;
        return [RCP_KCX + r * Math.cos(rad), RCP_KCY + r * Math.sin(rad)];
    };
    function rcpArc(from, to, r) {
        const a = rcpPolar(from, r), b = rcpPolar(to, r);
        const large = (to - from) > 180 ? 1 : 0;
        return `M${a[0].toFixed(1)},${a[1].toFixed(1)} A${r},${r} 0 ${large} 1 ${b[0].toFixed(1)},${b[1].toFixed(1)}`;
    }
    function rcpKnobSvg(v, min, max, color, size) {
        const ang = rcpKnobAng(v, min, max);
        const p = rcpPolar(ang, RCP_KR - 3), i = rcpPolar(ang, 13);
        const h = Math.round(size * 70 / 74);
        return `<svg class="rcp-knob-svg" width="${size}" height="${h}" viewBox="0 0 74 70">
            <circle cx="37" cy="34" r="30" fill="var(--rcp-hub-out)" stroke="var(--rcp-edge2)"/>
            <circle cx="37" cy="34" r="21" fill="var(--rcp-hub-in)" stroke="var(--rcp-edge2)"/>
            <path d="${rcpArc(RCP_KA0, RCP_KA1, RCP_KR)}" stroke="var(--rcp-edge2)" stroke-width="4" fill="none" stroke-linecap="round"/>
            <path class="rcp-arc" d="${rcpArc(RCP_KA0, ang, RCP_KR)}" stroke="${color}" stroke-width="4" fill="none" stroke-linecap="round"/>
            <line class="rcp-ptr" x1="${i[0].toFixed(1)}" y1="${i[1].toFixed(1)}" x2="${p[0].toFixed(1)}" y2="${p[1].toFixed(1)}" stroke="${color}" stroke-width="3" stroke-linecap="round"/>
          </svg>`;
    }
    // Police de la valeur : rétrécit à 4-5 chiffres pour rester dans le rotatif.
    const rcpFont = (val, base) => { const n = String(val).length; return n >= 5 ? base - 4 : n >= 4 ? base - 2.5 : base; };
    const rcpValInner = (p, val) => `${esc(val)}${p.unit ? `<span class="rcp-u">${esc(p.unit)}</span>` : ""}`;

    // Décalage int vs référence de CETTE caméra : pastille signée (bleu +, rose −, vert = réf).
    function rcpDeltaBadge(p, camId, key, val) {
        if (rcpValueMode === "ref") return "";   // on affiche déjà la référence : pas de décalage
        const ref = rcpRefVal(camId, key);
        if (ref === undefined) return "";
        const c = rcpNum(val), r = rcpNum(ref);
        if (isNaN(c) || isNaN(r)) return "";
        const d = c - r, cls = d > 0 ? "pos" : d < 0 ? "neg" : "zero";
        return `<span class="rcp-delta ${cls}" title="${esc(tr("plugin.ptz.rcp.ref", "référence"))} : ${esc(r)}">${d > 0 ? "+" : ""}${d}</span>`;
    }
    // Décalage enum/bool/text : pastille « modifié » si différent, « = » discret si à la réf.
    function rcpModBadge(camId, key, val) {
        if (rcpValueMode === "ref") return "";   // on affiche déjà la référence : pas de décalage
        const ref = rcpRefVal(camId, key);
        if (ref === undefined) return "";
        if (val === ref) return `<span class="rcp-mod zero">=</span>`;
        return `<span class="rcp-mod on" title="${esc(tr("plugin.ptz.rcp.ref", "référence"))} : ${esc(fmtVal(ref))}">${esc(tr("plugin.ptz.rcp.modified", "modifié"))}</span>`;
    }

    // Libellé d'une ligne trio, dérivé des membres : préfixe de mots communs + canaux présents
    // (« Pedestal rouge »/« Pedestal bleu » → « Pedestal R·B »).
    function rcpWordPrefix(labels) {
        if (!labels.length) return "";
        const split = labels.map((s) => String(s).trim().split(/\s+/));
        const first = split[0], out = [];
        for (let i = 0; i < first.length; i++) {
            const w = first[i];
            if (split.every((a) => a[i] === w)) out.push(w); else break;
        }
        return out.join(" ");
    }
    function rcpTripletLabel(labels, channels) {
        const chans = channels.slice().sort((a, b) => rcpChanOrder(a) - rcpChanOrder(b))
            .map((ch) => RCP_CH[ch] ? RCP_CH[ch].L : ch).join("·");
        const base = rcpWordPrefix(labels);
        return base ? `${base} ${chans}` : (chans || (labels[0] || ""));
    }

    // Union ordonnée des réglages : groupe → lignes. Une ligne est SOIT « simple » (une clé),
    // SOIT « trio » (tous les paramètres partageant un même `triplet` dans ce groupe). Ordre de
    // première apparition (caméras triées, puis ordre du schéma).
    function rcpBuildGroups(list) {
        const order = [], gmap = {};
        list.forEach((c) => {
            const dd = rcpData[c.id];
            if (!dd) return;
            Object.keys(dd.byKey).forEach((key) => {
                const p = dd.byKey[key], g = p.group || "";
                let G = gmap[g];
                if (!G) { G = gmap[g] = { name: g, rows: [], rmap: {} }; order.push(G); }
                if (p.triplet) {
                    const rid = "t:" + g + ":" + p.triplet;
                    let row = G.rmap[rid];
                    if (!row) {
                        row = G.rmap[rid] = { id: rid, kind: "triplet", group: g, triplet: p.triplet,
                            labels: new Set(), channels: new Set() };
                        G.rows.push(row);
                    }
                    row.labels.add(p.label);
                    if (p.channel) row.channels.add(p.channel);
                } else {
                    const rid = "k:" + g + ":" + key;
                    if (!G.rmap[rid]) {
                        G.rmap[rid] = { id: rid, kind: "simple", group: g, key: key, label: p.label, unit: p.unit };
                        G.rows.push(G.rmap[rid]);
                    }
                }
            });
        });
        order.forEach((G) => G.rows.forEach((row) => {
            if (row.kind === "triplet") row.label = rcpTripletLabel([...row.labels], [...row.channels]);
        }));
        return order;
    }

    // ── Disposition « pupitre RCP-3500 » : une TRANCHE (colonne) par caméra ────────────
    // Le PLACEMENT vient du champ `role` du descripteur (jamais de la marque) :
    //   role "iris"   → Diaph (bas gauche, F-stop)   ·  role "mblack" → Pedestal (bas droite)
    //   role "white"  → zone Blancs (par channel)    ·  role "black"  → zone Noirs (par channel)
    //   SANS role     → ÉCRAN LCD, regroupé par `group` (les groupes deviennent les onglets).

    // Noms COURTS des onglets de l'écran, dérivés des `group` réellement présents.
    const RCP_SHORT = {
        "Balance des blancs": "Bal. B", "Balance": "Bal.", "Exposition": "Expo", "Gamma": "Gamma",
        "Knee": "Knee", "Détail": "Détail", "Detail": "Détail", "Matrice": "Matrice", "Matriçage": "Matrice",
        "Look": "Look", "Saturation": "Sat.", "Chroma": "Chroma", "Obturateur": "Obtur.", "Gain": "Gain",
        "Noir": "Noir", "Filtre ND": "ND", "Skin": "Skin",
    };
    function rcpShort(g) {
        if (!g) return tr("plugin.ptz.rcp.other", "Autres");
        if (RCP_SHORT[g]) return RCP_SHORT[g];
        const w = String(g).split(/\s+/)[0];
        return w.length > 9 ? w.slice(0, 8) + "." : w;
    }

    // Paramètres couleur d'UNE caméra pour un rôle donné, triés par canal (R·V·B).
    function rcpRoleParams(c, role) {
        const dd = rcpData[c.id];
        if (!dd) return [];
        return Object.keys(dd.byKey).map((k) => dd.byKey[k]).filter((p) => p.role === role)
            .sort((a, b) => rcpChanOrder(a.channel) - rcpChanOrder(b.channel));
    }
    const rcpRoleOne = (c, role) => rcpRoleParams(c, role)[0] || null;
    // Réglages de l'ÉCRAN (sans rôle) d'une caméra pour une catégorie (nom de groupe).
    function rcpScreenParams(c, catg) {
        const dd = rcpData[c.id];
        if (!dd) return [];
        return Object.keys(dd.byKey).map((k) => dd.byKey[k]).filter((p) => !p.role && (p.group || "") === catg);
    }
    const rcpFavParams = (c) => {
        const dd = rcpData[c.id];
        return dd ? Object.keys(dd.byKey).map((k) => dd.byKey[k]).filter((p) => rcpPins.has(p.key)) : [];
    };

    // Onglets de l'écran = union ordonnée des `group` des réglages SANS rôle (première apparition).
    function rcpCategories(list) {
        const order = [], seen = new Set();
        list.forEach((c) => {
            const dd = rcpData[c.id];
            if (!dd) return;
            Object.keys(dd.byKey).forEach((k) => {
                const p = dd.byKey[k];
                if (p.role) return;
                const g = p.group || "";
                if (!seen.has(g)) { seen.add(g); order.push(g); }
            });
        });
        return order;
    }

    function renderRcp() {
        const body = $("#ptz-rcp-body");
        if (!body) return;
        if (rcpDragActive) { rcpRenderPending = true; return; }   // ne pas casser un glissement
        updateRcpMeta(); syncRcpToggles();
        const list = rcpCams();
        if (!list.length) {
            body.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.rcp.none",
                "Aucune caméra de presta (numérotée) à afficher."))}</div>`;
            return;
        }
        // Tant qu'AUCUNE caméra n'a répondu (ni valeurs, ni erreur), on annonce la lecture.
        // Dès qu'au moins une a un état, on dresse le rack : chaque tranche décrit alors le
        // sien (valeurs / lecture… / injoignable), une caméra morte ne bloque pas les autres.
        const anySettled = list.some((c) => rcpData[c.id] || rcpErr[c.id]);
        if (!anySettled) {
            body.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.reading", "Lecture…"))}</div>`;
            return;
        }
        const cats = rcpCategories(list);
        if (rcpCat !== "fav" && cats.indexOf(rcpCat) < 0) rcpCat = cats.length ? cats[0] : "fav";
        const compact = rcpViewMode === "compact";
        const catsBar = compact ? "" : rcpCatsBar(cats);
        const rack = `<div class="rcp-rack">${list.map((c) => rcpStrip(c, compact)).join("")}</div>`;
        body.innerHTML = `<div class="rcp-console${rcpReadonly() ? " rcp-refmode" : ""}">${catsBar}${rack}</div>`;
        rcpBindWall(body);
        rcpPositionOps(body);
        applyStatusDots();
    }

    // Barre d'onglets de l'écran (★ Favoris en tête), noms courts, flex-wrap (multi-lignes).
    function rcpCatsBar(cats) {
        const chip = (id, label, on) =>
            `<button class="rcp-chip ${on ? "on" : ""}" data-cat="${esc(id)}" type="button">${esc(label)}</button>`;
        return `<div class="rcp-cats"><span class="rcp-catlbl">${esc(tr("plugin.ptz.rcp.screen", "Écran"))}</span>`
            + chip("fav", "★ " + tr("plugin.ptz.rcp.favShort", "Favoris"), rcpCat === "fav")
            + cats.map((g) => chip(g, rcpShort(g), rcpCat === g)).join("")
            + `</div>`;
    }

    // Une tranche caméra (colonne) : en-tête → écran → Blancs → Noirs → Diaph + Pedestal.
    // En vue compacte : en-tête + Diaph + Pedestal seulement (pour piloter beaucoup de caméras).
    function rcpStrip(c, compact) {
        const num = c.cam_number != null ? `N°${esc(c.cam_number)}` : "";
        const model = (c.identity && c.identity.model) || c.model || "";
        const err = rcpErr[c.id];
        const head = `<div class="rcp-shead">
            <div class="rcp-sh-id"><span class="ptz-dot" data-dot-for="${esc(c.id)}"></span>
              <div class="rcp-sh-txt">
                <span class="rcp-sh-nm">${num ? num + " " : ""}${esc(c.name)}</span>
                <span class="rcp-sh-md">${err ? esc(tr("plugin.ptz.unreachable", "injoignable")) : esc(model)}</span>
              </div></div>
            <span class="rcp-tally" title="tally"></span></div>`;
        let inner;
        if (err) {
            inner = `<div class="rcp-strip-err" title="${esc(err)}">${esc(tr("plugin.ptz.unreachable", "injoignable"))}</div>`
                + `<div class="rcp-op-zone">${rcpDiaphHtml(c)}${rcpPedHtml(c)}</div>`;
        } else if (!rcpData[c.id]) {
            inner = `<div class="rcp-strip-load">${esc(tr("plugin.ptz.reading", "Lecture…"))}</div>`;
        } else {
            const top = compact ? "" :
                rcpScreenHtml(c)
                + rcpZoneHtml(c, "white", tr("plugin.ptz.rcp.whites", "Blancs"), "var(--rcp-amber)")
                + rcpZoneHtml(c, "black", tr("plugin.ptz.rcp.blacks", "Noirs"), "var(--rcp-muted)");
            inner = top + `<div class="rcp-op-zone">${rcpDiaphHtml(c)}${rcpPedHtml(c)}</div>`;
        }
        return `<div class="rcp-strip ${err ? "err" : ""}">${head}${inner}</div>`;
    }

    // L'ÉCRAN LCD : onglet ★ Favoris (réglages épinglés) ou catégorie courante (par groupe).
    function rcpScreenHtml(c) {
        const isFav = rcpCat === "fav";
        const params = isFav ? rcpFavParams(c) : rcpScreenParams(c, rcpCat);
        const title = isFav ? "★ " + tr("plugin.ptz.rcp.favShort", "Favoris") : rcpShort(rcpCat);
        let grid;
        if (!params.length) {
            grid = isFav
                ? `<div class="rcp-fav-empty">${esc(tr("plugin.ptz.rcp.favEmpty",
                    "Aucun favori. Cliquez l'étoile ★ d'un réglage pour l'ajouter."))}</div>`
                : `<div class="rcp-fav-empty">${esc(tr("plugin.ptz.rcp.catEmpty", "—"))}</div>`;
        } else {
            grid = `<div class="rcp-kgrid">${params.map((p) => rcpMini(c, p, true)).join("")}</div>`;
        }
        return `<div class="rcp-screen">
            <div class="rcp-screen-lbl"><span>${esc(title)}</span><span class="rcp-screen-pg">${params.length || ""}</span></div>
            ${grid}</div>`;
    }

    // Zone permanente Blancs / Noirs : les canaux R/(V/)B côte à côte, colorés.
    function rcpZoneHtml(c, role, title, dot) {
        const params = rcpRoleParams(c, role);
        const inner = params.length
            ? `<div class="rcp-zrow">${params.map((p) => rcpMini(c, p, false)).join("")}</div>`
            : `<div class="rcp-zna">—</div>`;
        return `<div class="rcp-zone rcp-zone-${role}">
            <div class="rcp-ztl"><span class="rcp-zdot" style="background:${dot}"></span>${esc(title)}</div>
            ${inner}</div>`;
    }

    // Un mini-contrôle (écran, blancs, noirs) : étoile Favori + contrôle + libellé optionnel.
    function rcpMini(c, p, withLabel) {
        const on = rcpPins.has(p.key);
        const star = `<button class="rcp-star ${on ? "on" : ""}" data-fav="${esc(p.key)}" type="button"
            title="${esc(tr("plugin.ptz.rcp.fav", "Favori (onglet ★)"))}">${on ? "★" : "☆"}</button>`;
        const label = withLabel ? `<div class="rcp-mlabel">${esc(p.label)}</div>` : "";
        return `<div class="rcp-mini">${star}${label}${rcpScreenControl(c, p)}</div>`;
    }
    // Contrôle d'un mini : int → petit rotatif/stepper coloré ; sinon type déclaré (enum/bool/text).
    function rcpScreenControl(c, p) {
        if (!p.writable) return rcpRoControl(c, p);
        if (p.type === "int") return rcpNumControl(c, p, true);
        return rcpControl(c, p);
    }

    // ── Diaph (rôle "iris") ────────────────────────────────────────────────────────────
    // Valeur affichée en F-stop (approximatif, interpolé entre min/max du param). Le contrôle
    // écrit la valeur BRUTE. Panasonic n'a pas d'iris → emplacement vide « — ».
    function rcpDiaphHtml(c) {
        const p = rcpRoleOne(c, "iris");
        const lab = tr("plugin.ptz.rcp.diaph", "Diaph");
        if (!p) return `<div class="rcp-op rcp-op-diaph rcp-op-empty"><div class="rcp-oplab">${esc(lab)}</div><div class="rcp-op-na">—</div></div>`;
        return rcpOpHtml(c, p, "diaph", lab, rcpDiaphStyle, 86);
    }
    // ── Pedestal (rôle "mblack") ───────────────────────────────────────────────────────
    function rcpPedHtml(c) {
        const p = rcpRoleOne(c, "mblack");
        const lab = tr("plugin.ptz.rcp.ped", "Pedestal");
        if (!p) return `<div class="rcp-op rcp-op-ped rcp-op-empty"><div class="rcp-oplab">${esc(lab)}</div><div class="rcp-op-na">—</div></div>`;
        return rcpOpHtml(c, p, "ped", lab, rcpPedStyle, 68);
    }
    // Fabrique commune d'un contrôle d'exploitation (Diaph/Pedestal). `style` : diaph =
    // fader|knob, ped = wheel|knob. Écrit la valeur brute via le pipeline existant (data-ctl-*).
    function rcpOpHtml(c, p, kind, lab, style, knobSize) {
        const val = rcpCellVal(c.id, p.key);
        const unread = val === null || val === undefined;
        const off = rcpOpOffset(p, c.id, val, kind);
        const numTxt = unread ? "—" : rcpOpNum(kind, p, val);
        let ctrl;
        if (unread) ctrl = `<div class="rcp-op-na">–</div>`;
        else if (style === "knob") ctrl = `<div class="rcp-opknob">${rcpKnobSvg(val, p.min, p.max, "var(--rcp-amber)", knobSize)}</div>`;
        else if (kind === "diaph") ctrl = `<div class="rcp-fader"><div class="rcp-fader-fill"></div><div class="rcp-fader-cap"></div></div>`;
        else ctrl = `<div class="rcp-wheel"><div class="rcp-wheel-ridges"></div><div class="rcp-wheel-cur"></div></div>`;
        return `<div class="rcp-op rcp-op-${kind}${off.ring ? " rcp-op-off" : ""}"
            data-ctl-cam="${esc(c.id)}" data-ctl-key="${esc(p.key)}" data-ptype="int" data-opkind="${kind}" data-style="${style}">
            <div class="rcp-oplab">${esc(lab)}</div>
            ${ctrl}
            <div class="rcp-opnum"><span class="rcp-opval">${esc(numTxt)}</span>${off.html}</div></div>`;
    }
    const rcpOpNum = (kind, p, val) => kind === "diaph" ? rcpFStop(val, p) : ((rcpNum(val) > 0 ? "+" : "") + val);

    // Écart vs référence pour un contrôle d'exploitation : liseré (ring) + pastille. Diaph →
    // la cible en F-stop ; Pedestal → l'écart signé (brut). Rien en mode « Référence ».
    function rcpOpOffset(p, camId, val, kind) {
        if (rcpValueMode === "ref") return { ring: false, html: "" };
        const ref = rcpRefVal(camId, p.key);
        if (ref === undefined) return { ring: false, html: "" };
        const cur = rcpNum(val), r = rcpNum(ref);
        if (isNaN(cur) || isNaN(r)) return { ring: false, html: "" };
        const d = cur - r;
        if (d === 0) return { ring: false, html: `<span class="rcp-opoff zero">=</span>` };
        const cls = d > 0 ? "pos" : "neg";
        const txt = kind === "diaph" ? "→ " + rcpFStop(ref, p) : (d > 0 ? "+" : "") + d;
        const ttv = kind === "diaph" ? rcpFStop(ref, p) : r;
        return { ring: true, html: `<span class="rcp-opoff ${cls}" title="${esc(tr("plugin.ptz.rcp.ref", "référence"))} : ${esc(ttv)}">${esc(txt)}</span>` };
    }

    // ── F-stop du diaph (APPROXIMATIF) ─────────────────────────────────────────────────
    // Interpolation LINÉAIRE en espace APEX (Av = 2·log2(N)) entre l'extrémité fermée (min du
    // param → F22) et l'extrémité ouverte (max → F4). Ce mapping reproduit à ~2 % près les
    // ancres FR7 confirmées en direct (31743≈F4, 31487≈F5.6, 30975≈F11, 30464≈F22). La valeur
    // écrite reste la valeur BRUTE ; on ne fait qu'AFFICHER le F-stop, puis on cale sur le cran
    // standard le plus proche (comme le fait la caméra). À revoir si une caméra ouvre plus que F4.
    const RCP_FSTOPS = [1.4, 1.9, 2, 2.8, 4, 5.6, 8, 11, 16, 22];
    const RCP_AV_OPEN = 2 * Math.log2(4), RCP_AV_CLOSED = 2 * Math.log2(22);
    function rcpFStop(val, p) {
        // Certaines caméras (ex. CCU Sony) n'exposent que la COMMANDE d'iris, pas le F-number
        // objectif : le param déclare alors une unité "%" et on affiche l'ouverture en %, pas un
        // F-stop faux. La position du fader (rcpNorm) reste basée sur min/max, inchangée.
        if (p && p.unit === "%") return rcpNum(val) + "%";
        const lo = p.min, hi = p.max;
        if (lo == null || hi == null || hi <= lo) return String(val);
        let t = (rcpNum(val) - lo) / (hi - lo);          // 0 = fermé (min), 1 = ouvert (max)
        if (isNaN(t)) return "—";
        t = Math.max(0, Math.min(1, t));
        const av = RCP_AV_CLOSED + t * (RCP_AV_OPEN - RCP_AV_CLOSED);
        const N = Math.pow(2, av / 2);
        let best = RCP_FSTOPS[0], bd = Infinity;
        RCP_FSTOPS.forEach((f) => { const dd = Math.abs(Math.log(f) - Math.log(N)); if (dd < bd) { bd = dd; best = f; } });
        return "F" + best;
    }

    // ── Positionnement des faders/molettes (après rendu, et à chaque glissement) ────────
    function rcpNorm(p, val) {
        const lo = p.min == null ? 0 : p.min, hi = p.max == null ? 100 : p.max;
        let t = hi > lo ? (rcpNum(val) - lo) / (hi - lo) : 0;
        if (isNaN(t)) t = 0;
        return Math.max(0, Math.min(1, t));
    }
    function rcpFaderPos(fader, t) {
        const h = fader.clientHeight || 170;
        const cap = fader.querySelector(".rcp-fader-cap"), fill = fader.querySelector(".rcp-fader-fill");
        if (!cap || !fill) return;
        const capH = 26, pad = 5, top = pad + (h - capH - 2 * pad) * (1 - t);
        cap.style.top = top + "px"; fill.style.top = (top + capH / 2) + "px"; fill.style.bottom = pad + "px";
    }
    function rcpWheelPos(wheel, t) {
        const h = wheel.clientHeight || 140, cur = wheel.querySelector(".rcp-wheel-cur");
        if (!cur) return;
        const pad = 6; cur.style.top = (pad + (h - 2 * pad) * (1 - t)) + "px";
    }
    function rcpPositionOps(body) {
        body.querySelectorAll(".rcp-op").forEach((op) => {
            const cam = op.dataset.ctlCam, key = op.dataset.ctlKey;
            if (!cam) return;
            const dd = rcpData[cam], p = dd && dd.byKey[key];
            if (!p) return;
            const val = rcpCellVal(cam, key);
            if (val === null || val === undefined) return;
            const t = rcpNorm(p, val);
            const f = op.querySelector(".rcp-fader"); if (f) rcpFaderPos(f, t);
            const w = op.querySelector(".rcp-wheel"); if (w) rcpWheelPos(w, t);
        });
    }
    // Repeint un contrôle d'exploitation sans reconstruire (garde vivant le glissement).
    function rcpPaintOp(wrap, p, val) {
        const kind = wrap.dataset.opkind, t = rcpNorm(p, val);
        const f = wrap.querySelector(".rcp-fader"); if (f) rcpFaderPos(f, t);
        const w = wrap.querySelector(".rcp-wheel"); if (w) rcpWheelPos(w, t);
        const sv = wrap.querySelector(".rcp-knob-svg");
        if (sv) {
            const ang = rcpKnobAng(val, p.min, p.max);
            const arc = sv.querySelector(".rcp-arc"); if (arc) arc.setAttribute("d", rcpArc(RCP_KA0, ang, RCP_KR));
            const ptr = sv.querySelector(".rcp-ptr");
            if (ptr) {
                const a = rcpPolar(ang, RCP_KR - 3), b = rcpPolar(ang, 13);
                ptr.setAttribute("x1", b[0].toFixed(1)); ptr.setAttribute("y1", b[1].toFixed(1));
                ptr.setAttribute("x2", a[0].toFixed(1)); ptr.setAttribute("y2", a[1].toFixed(1));
            }
        }
        const off = rcpOpOffset(p, wrap.dataset.ctlCam, val, kind);
        wrap.classList.toggle("rcp-op-off", off.ring);
        const num = wrap.querySelector(".rcp-opnum");
        if (num) num.innerHTML = `<span class="rcp-opval">${esc(rcpOpNum(kind, p, val))}</span>${off.html}`;
    }

    function rcpControl(c, p) {
        if (!p.writable) return rcpRoControl(c, p);
        if (p.type === "int") return rcpNumControl(c, p, false);
        if (p.type === "bool") return rcpBoolControl(c, p);
        if (p.type === "enum") return rcpEnumControl(c, p);
        return rcpTextControl(c, p);
    }

    // Contrôle int : rotatif (arc + glisser/molette) OU stepper vertical (+ au-dessus, − en
    // dessous), selon la bascule « Contrôle ». `mini` = un canal d'un trio (petit, coloré).
    function rcpNumControl(c, p, mini) {
        const camId = c.id, key = p.key;
        const val = rcpCellVal(camId, key);
        if (!p.writable) return rcpRoControl(c, p);       // canal d'un trio en lecture seule
        const chan = mini ? RCP_CH[p.channel] : null;
        const color = chan ? chan.c : "var(--rcp-amber)";
        const cap = chan ? chan.L : "";
        const attrs = `data-ctl-cam="${esc(camId)}" data-ctl-key="${esc(key)}" data-ptype="int"`;
        if (val === null || val === undefined) {
            return `<div class="rcp-mini-na" ${attrs} title="${esc(tr("plugin.ptz.notRead", "non lu"))}">–${cap
                ? ` <span class="rcp-cap" style="color:${color}">${esc(cap)}</span>` : ""}</div>`;
        }
        const base = mini ? 12 : 15;
        const valHtml = `<span class="rcp-val" data-editable="1" style="font-size:${rcpFont(val, base)}px">${rcpValInner(p, val)}</span>`;
        const delta = `<div class="rcp-delta-slot">${rcpDeltaBadge(p, camId, key, val)}</div>`;
        const capHtml = cap ? `<div class="rcp-cap" style="color:${color}">${esc(cap)}</div>` : "";
        if (rcpStyle === "knob") {
            const size = mini ? 54 : 74, dh = Math.round(size * 70 / 74);
            return `<div class="rcp-knob ${mini ? "mini" : "big"}" ${attrs} style="--c:${color};--rcp-dh:${dh}px">
                ${delta}${rcpKnobSvg(val, p.min, p.max, color, size)}
                <div class="rcp-valwrap">${valHtml}</div>${capHtml}</div>`;
        }
        return `<div class="rcp-stp ${mini ? "mini" : "big"}" ${attrs} style="--c:${color}" tabindex="0">
            ${delta}
            <button class="rcp-sbtn rcp-up" data-d="1" tabindex="-1" type="button">+</button>
            ${valHtml}
            <button class="rcp-sbtn rcp-dn" data-d="-1" tabindex="-1" type="button">−</button>
            ${capHtml}</div>`;
    }

    function rcpEnumControl(c, p) {
        const camId = c.id, key = p.key, val = rcpCellVal(camId, key);
        const unread = val === null || val === undefined;
        const opts = (p.options || []).map((o) =>
            `<option value="${esc(o.value)}" ${o.value === val ? "selected" : ""}>${esc(o.label)}</option>`).join("");
        return `<div class="rcp-sel" data-ctl-cam="${esc(camId)}" data-ctl-key="${esc(key)}" data-ptype="enum">
            <select><option value="">${esc(unread ? tr("plugin.ptz.notRead", "non lu") : "—")}</option>${opts}</select>
            <div class="rcp-cap">${rcpModBadge(camId, key, val)}</div></div>`;
    }

    function rcpBoolControl(c, p) {
        const camId = c.id, key = p.key, val = rcpCellVal(camId, key);
        return `<div class="rcp-tgl" data-ctl-cam="${esc(camId)}" data-ctl-key="${esc(key)}" data-ptype="bool">
            <div class="rcp-switch ${val === true ? "on" : ""}" role="switch" aria-checked="${val === true}"></div>
            <div class="rcp-cap">${rcpModBadge(camId, key, val)}</div></div>`;
    }

    function rcpTextControl(c, p) {
        const camId = c.id, key = p.key, val = rcpCellVal(camId, key);
        const unread = val === null || val === undefined;
        return `<div class="rcp-txt" data-ctl-cam="${esc(camId)}" data-ctl-key="${esc(key)}" data-ptype="text">
            <input type="text" class="rcp-tin" value="${unread ? "" : esc(val)}" placeholder="${esc(tr("plugin.ptz.notRead", "non lu"))}">
            <div class="rcp-cap">${rcpModBadge(camId, key, val)}</div></div>`;
    }

    function rcpRoControl(c, p) {
        const val = rcpCellVal(c.id, p.key);
        if (val === null || val === undefined) return `<div class="rcp-mini-na">–</div>`;
        return `<div class="rcp-ro" title="${esc(tr("plugin.ptz.unvalidated", "lecture seule"))}">${esc(fmtVal(val))}</div>`;
    }

    // ── Interactions du mur RCP ──────────────────────────────
    function rcpBindWall(body) {
        // Onglets de l'écran (catégories + ★ Favoris).
        body.querySelectorAll(".rcp-chip").forEach((el) =>
            el.addEventListener("click", () => rcpSetCat(el.dataset.cat)));
        // Étoiles Favori (sur les minis blancs/noirs/écran).
        body.querySelectorAll(".rcp-star").forEach((el) =>
            el.addEventListener("click", (e) => { e.stopPropagation(); rcpToggleFav(el.dataset.fav); }));
        // Diaph / Pedestal : fader, molette ou rotatif → même pipeline de glissement que les rotatifs.
        body.querySelectorAll(".rcp-op").forEach((op) => {
            const dd = rcpData[op.dataset.ctlCam], p = dd && dd.byKey[op.dataset.ctlKey];
            if (!p || !p.writable) return;
            const start = (e) => rcpKnobDown(e, op);
            [".rcp-fader", ".rcp-wheel", ".rcp-knob-svg"].forEach((sel) => {
                const t = op.querySelector(sel);
                if (t) { t.addEventListener("mousedown", start); t.addEventListener("touchstart", start, { passive: false }); }
            });
            op.addEventListener("wheel", (e) => { e.preventDefault();
                rcpStep(op.dataset.ctlCam, op.dataset.ctlKey, e.deltaY < 0 ? 1 : -1, e.altKey || e.shiftKey); }, { passive: false });
        });
        body.querySelectorAll(".rcp-val[data-editable]").forEach((el) =>
            el.addEventListener("click", () => rcpEditValue(el)));
        body.querySelectorAll(".rcp-knob").forEach((wrap) => {
            const svg = wrap.querySelector(".rcp-knob-svg");
            if (svg) {
                svg.addEventListener("mousedown", (e) => rcpKnobDown(e, wrap));
                svg.addEventListener("touchstart", (e) => rcpKnobDown(e, wrap), { passive: false });
            }
            wrap.addEventListener("wheel", (e) => { e.preventDefault();
                rcpStep(wrap.dataset.ctlCam, wrap.dataset.ctlKey, e.deltaY < 0 ? 1 : -1, e.altKey || e.shiftKey); }, { passive: false });
        });
        body.querySelectorAll(".rcp-stp").forEach((wrap) => {
            wrap.querySelectorAll(".rcp-sbtn").forEach((b) =>
                b.addEventListener("click", (e) => rcpStep(wrap.dataset.ctlCam, wrap.dataset.ctlKey, +b.dataset.d, e.altKey || e.shiftKey)));
            wrap.addEventListener("wheel", (e) => { e.preventDefault();
                rcpStep(wrap.dataset.ctlCam, wrap.dataset.ctlKey, e.deltaY < 0 ? 1 : -1, e.altKey || e.shiftKey); }, { passive: false });
            wrap.addEventListener("keydown", (e) => {
                if (e.key === "ArrowUp") { e.preventDefault(); rcpStep(wrap.dataset.ctlCam, wrap.dataset.ctlKey, 1, e.altKey || e.shiftKey); }
                else if (e.key === "ArrowDown") { e.preventDefault(); rcpStep(wrap.dataset.ctlCam, wrap.dataset.ctlKey, -1, e.altKey || e.shiftKey); }
            });
        });
        body.querySelectorAll(".rcp-switch").forEach((el) =>
            el.addEventListener("click", () => {
                const w = el.closest("[data-ctl-cam]"), dd = rcpData[w.dataset.ctlCam];
                rcpCommitDiscrete(w.dataset.ctlCam, w.dataset.ctlKey, !(dd.values[w.dataset.ctlKey] === true));
            }));
        body.querySelectorAll(".rcp-sel select").forEach((el) =>
            el.addEventListener("change", () => {
                const w = el.closest("[data-ctl-cam]");
                if (!el.value) return;                          // « — »/« non lu » n'écrit rien
                rcpCommitDiscrete(w.dataset.ctlCam, w.dataset.ctlKey, el.value);
            }));
        body.querySelectorAll(".rcp-txt input").forEach((el) =>
            el.addEventListener("change", () => {
                const w = el.closest("[data-ctl-cam]");
                rcpCommitDiscrete(w.dataset.ctlCam, w.dataset.ctlKey, el.value);
            }));
    }

    function rcpFindWrap(camId, key) {
        if (!root) return null;
        let found = null;
        root.querySelectorAll(".rcp-console [data-ctl-cam]").forEach((w) => {
            if (!found && w.dataset.ctlCam === camId && w.dataset.ctlKey === key) found = w;
        });
        return found;
    }

    // Repeint UNE valeur sans reconstruire le DOM : garde vivants le glissement et le focus.
    function rcpPaintValue(wrap, p, val) {
        if (!wrap) return;
        if (wrap.classList.contains("rcp-op")) { rcpPaintOp(wrap, p, val); return; }   // Diaph/Pedestal
        const camId = wrap.dataset.ctlCam, key = wrap.dataset.ctlKey;
        const base = wrap.classList.contains("mini") ? 12 : 15;
        const vt = wrap.querySelector(".rcp-val");
        if (vt) { vt.innerHTML = rcpValInner(p, val); vt.style.fontSize = rcpFont(val, base) + "px"; }
        const slot = wrap.querySelector(".rcp-delta-slot");
        if (slot) slot.innerHTML = rcpDeltaBadge(p, camId, key, val);
        if (wrap.classList.contains("rcp-knob")) {
            const ang = rcpKnobAng(val, p.min, p.max);
            const arc = wrap.querySelector(".rcp-arc");
            if (arc) arc.setAttribute("d", rcpArc(RCP_KA0, ang, RCP_KR));
            const ptr = wrap.querySelector(".rcp-ptr");
            if (ptr) {
                const a = rcpPolar(ang, RCP_KR - 3), b = rcpPolar(ang, 13);
                ptr.setAttribute("x1", b[0].toFixed(1)); ptr.setAttribute("y1", b[1].toFixed(1));
                ptr.setAttribute("x2", a[0].toFixed(1)); ptr.setAttribute("y2", a[1].toFixed(1));
            }
        }
    }

    // Saisie clavier au clic sur la valeur : Entrée valide, Échap annule.
    function rcpEditValue(el) {
        if (rcpReadonly()) return;                        // mode « Référence » : lecture seule
        const wrap = el.closest("[data-ctl-cam]");
        if (!wrap) return;
        const camId = wrap.dataset.ctlCam, key = wrap.dataset.ctlKey;
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p || !p.writable) return;
        const cur = dd.values[key];
        const inp = document.createElement("input");
        inp.type = "number"; inp.className = "rcp-vedit";
        inp.value = (cur === null || cur === undefined) ? "" : cur;
        if (p.min != null) inp.min = p.min;
        if (p.max != null) inp.max = p.max;
        if (p.step != null) inp.step = p.step;
        el.replaceWith(inp); inp.focus(); inp.select();
        let done = false;
        const finish = (commit) => {
            if (done) return; done = true;
            if (commit) { const nv = parseFloat(inp.value); if (!isNaN(nv)) dd.values[key] = rcpClamp(p, nv); }
            const val = dd.values[key];
            const span = document.createElement("span");
            span.className = "rcp-val"; span.setAttribute("data-editable", "1");
            span.style.fontSize = rcpFont(val, wrap.classList.contains("mini") ? 12 : 15) + "px";
            span.innerHTML = rcpValInner(p, val);
            span.addEventListener("click", () => rcpEditValue(span));
            inp.replaceWith(span);
            rcpPaintValue(wrap, p, val);
            if (commit) rcpWriteNow(camId, key);
        };
        inp.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); finish(true); }
            else if (e.key === "Escape") { e.preventDefault(); finish(false); }
        });
        inp.addEventListener("blur", () => finish(true));
    }

    function rcpSetLive(camId, key, val) {
        const dd = rcpData[camId];
        if (!dd) return;
        dd.values[key] = val;
        rcpPaintValue(rcpFindWrap(camId, key), dd.byKey[key], val);
        rcpQueueWrite(camId, key);
    }
    function rcpStep(camId, key, dir, big) {
        if (rcpReadonly()) return;                        // mode « Référence » : lecture seule
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p || !p.writable) return;
        rcpSetLive(camId, key, rcpStepVal(p, dd.values[key], dir, big));
    }

    // Glisser vertical du rotatif : les gestionnaires vivent au niveau document (posés au
    // montage) et lisent `rcpDrag` — repeindre le contrôle ne les casse donc pas.
    function rcpKnobDown(e, wrap) {
        if (rcpReadonly()) return;                        // mode « Référence » : lecture seule
        const camId = wrap.dataset.ctlCam, key = wrap.dataset.ctlKey;
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p || !p.writable) return;
        const cur = rcpNum(dd.values[key]);
        rcpDrag = { camId, key, moved: false,
            startY: (e.touches ? e.touches[0].clientY : e.clientY),
            startVal: isNaN(cur) ? (p.min != null ? p.min : 0) : cur };
        rcpDragActive = true;
        document.body.style.cursor = "ns-resize";
        if (e.cancelable) e.preventDefault();
    }
    function rcpOnDragMove(e) {
        if (!rcpDrag) return;
        const dd = rcpData[rcpDrag.camId], p = dd && dd.byKey[rcpDrag.key];
        if (!p) return;
        const y = (e.touches ? e.touches[0].clientY : e.clientY);
        const range = ((p.max != null ? p.max : 100) - (p.min != null ? p.min : 0)) || 100;
        const v = rcpClamp(p, rcpDrag.startVal + (rcpDrag.startY - y) / 150 * range);
        if (dd.values[rcpDrag.key] !== v) {
            rcpDrag.moved = true;
            dd.values[rcpDrag.key] = v;
            rcpPaintValue(rcpFindWrap(rcpDrag.camId, rcpDrag.key), p, v);
        }
        if (e.cancelable) e.preventDefault();
    }
    function rcpOnDragUp() {
        if (!rcpDrag) return;
        const camId = rcpDrag.camId, key = rcpDrag.key, moved = rcpDrag.moved;
        rcpDrag = null; rcpDragActive = false;
        document.body.style.cursor = "";
        if (moved) rcpQueueWrite(camId, key);
        if (rcpRenderPending) { rcpRenderPending = false; renderRcp(); }
    }

    // ── Écriture (débounce pour rotatifs/steppers, immédiate pour un choix délibéré) ──
    function rcpQueueWrite(camId, key) {
        const ck = camId + "|" + key;
        clearTimeout(rcpTimers[ck]);
        rcpTimers[ck] = setTimeout(() => { delete rcpTimers[ck]; rcpFlush(camId, key); }, 320);
    }
    async function rcpFlush(camId, key) {
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p) return;
        if (p.heavy && !confirm(tr("plugin.ptz.rcp.heavyConfirm",
            "Ce réglage fait REDÉMARRER la caméra (~2 min). Continuer ?"))) { await rcpReread(camId); return; }
        await rcpDoWrite(camId, key, true);
    }
    async function rcpWriteNow(camId, key) {          // saisie clavier validée
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p) return;
        const ck = camId + "|" + key; clearTimeout(rcpTimers[ck]); delete rcpTimers[ck];
        if (p.heavy && !confirm(tr("plugin.ptz.rcp.heavyConfirm",
            "Ce réglage fait REDÉMARRER la caméra (~2 min). Continuer ?"))) { await rcpReread(camId); return; }
        await rcpDoWrite(camId, key, false);
    }
    async function rcpCommitDiscrete(camId, key, val) {   // bool / enum / text
        if (rcpReadonly()) return;                        // mode « Référence » : lecture seule
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p || !p.writable) return;
        if (p.heavy && !confirm(tr("plugin.ptz.rcp.heavyConfirm",
            "Ce réglage fait REDÉMARRER la caméra (~2 min). Continuer ?"))) { fillRcpColumn(camId); return; }
        dd.values[key] = val;
        fillRcpColumn(camId);                             // reflète tout de suite le choix
        await rcpDoWrite(camId, key, false);
    }
    async function rcpDoWrite(camId, key, silentOk) {
        const dd = rcpData[camId], p = dd && dd.byKey[key];
        if (!p || !p.writable) return;
        const val = dd.values[key];
        try {
            const r = await ctx.api("cameras/" + camId + "/params", { body: { values: { [key]: val } } });
            const rep = (r.report || {})[key] || {};
            if (rep.ok) { if (!silentOk) toast(tr("plugin.ptz.written", "Paramètre appliqué"), "info"); }
            else toast(rep.error || tr("plugin.ptz.writeFail", "Écriture refusée"), "error");
        } catch (e) { toast(e.message, "error"); }
        await rcpReread(camId);                            // une écriture peut en contraindre d'autres
    }
    async function rcpReread(camId) {
        try {
            const d = await ctx.api("cameras/" + camId + "/params");
            rcpData[camId] = rcpColorOf(d); delete rcpErr[camId];
        } catch (e) { rcpErr[camId] = e.message; }
        fillRcpColumn(camId);
    }

    // Rafraîchit les contrôles d'UNE caméra en place (sans reconstruire le mur). Si la
    // STRUCTURE a changé (un réglage apparu/disparu, un « non lu » devenu lisible), on
    // reconstruit tout — sinon on repeint valeurs, arcs, interrupteurs et décalages.
    function fillRcpColumn(camId) {
        if (!root || rcpDragActive) return;
        const dd = rcpData[camId];
        let mismatch = false;
        root.querySelectorAll(".rcp-console [data-ctl-cam]").forEach((w) => {
            if (w.dataset.ctlCam !== camId) return;
            const key = w.dataset.ctlKey, p = dd && dd.byKey[key];
            if (!p) { mismatch = true; return; }
            const val = dd.values[key], ptype = w.dataset.ptype;
            if (w.classList.contains("rcp-op")) {                 // Diaph/Pedestal
                const has = !(val === null || val === undefined);
                const hadCtl = !!w.querySelector(".rcp-opval");
                if (has !== hadCtl) { mismatch = true; return; }
                if (has) rcpPaintOp(w, p, val);
            } else if (ptype === "int") {
                const hasCtl = !!w.querySelector(".rcp-val");
                const has = !(val === null || val === undefined);
                if (has !== hasCtl) { mismatch = true; return; }
                if (has) rcpPaintValue(w, p, val);
            } else if (ptype === "bool") {
                const sw = w.querySelector(".rcp-switch");
                if (sw) { sw.classList.toggle("on", val === true); sw.setAttribute("aria-checked", val === true); }
                const cap = w.querySelector(".rcp-cap"); if (cap) cap.innerHTML = rcpModBadge(camId, key, val);
            } else if (ptype === "enum") {
                const sel = w.querySelector("select"); if (sel) sel.value = (val === null || val === undefined) ? "" : val;
                const cap = w.querySelector(".rcp-cap"); if (cap) cap.innerHTML = rcpModBadge(camId, key, val);
            } else if (ptype === "text") {
                const inp = w.querySelector("input");
                if (inp && document.activeElement !== inp) inp.value = (val === null || val === undefined) ? "" : val;
                const cap = w.querySelector(".rcp-cap"); if (cap) cap.innerHTML = rcpModBadge(camId, key, val);
            }
        });
        if (mismatch) { renderRcp(); return; }
        updateRcpMeta();
    }

    // Mémorise l'état courant de TOUS les réglages couleur de TOUTES les caméras lues comme
    // référence { camId: { key: value } }, avec la date. Les valeurs non lues sont omises :
    // on ne fige pas une référence sur un « non lu ».
    async function rcpSetReference() {
        const list = rcpCams();
        const ref = {};
        let n = 0;
        list.forEach((c) => {
            const dd = rcpData[c.id];
            if (!dd) return;
            const m = {};
            Object.keys(dd.byKey).forEach((key) => {
                const v = dd.values[key];
                if (v !== null && v !== undefined) m[key] = v;
            });
            if (Object.keys(m).length) { ref[c.id] = m; n++; }
        });
        if (!n) { toast(tr("plugin.ptz.rcp.nothingToRef", "Rien à mémoriser (aucune valeur lue)."), "error"); return; }
        rcpRef = ref;
        rcpRefDate = new Date().toISOString();
        await rcpRefPersist({ date: rcpRefDate, ref });
        toast(`${tr("plugin.ptz.rcp.refSaved", "Référence mémorisée")} · ${n} ${tr("plugin.ptz.cameras", "caméra(s)")}`, "info");
        renderRcp();
    }

    async function rcpClearReference() {
        if (!rcpRef) return;
        if (!confirm(tr("plugin.ptz.rcp.clearConfirm", "Effacer la référence colorimétrique ?"))) return;
        await rcpRefRemove();
        rcpRef = null; rcpRefDate = null;
        toast(tr("plugin.ptz.rcp.refCleared", "Référence effacée"), "info");
        renderRcp();
    }

    function updateRcpMeta() {
        const el = $("#ptz-rcp-meta");
        if (!el) return;
        const list = rcpCams();
        const ok = list.filter((c) => rcpData[c.id]).length;
        const ko = list.filter((c) => rcpErr[c.id]).length;
        let s = (ok + ko < list.length)
            ? `${ok + ko}/${list.length} ${tr("plugin.ptz.read", "lue(s)")}…`
            : `${ok} ${tr("plugin.ptz.read", "lue(s)")}` + (ko ? ` · ${ko} ${tr("plugin.ptz.failed", "échec(s)")}` : "");
        s += " · " + (rcpRef
            ? tr("plugin.ptz.rcp.refSet", "référence :") + " " + (rcpRefDate ? fmtDate2(rcpRefDate) : "✓")
            : tr("plugin.ptz.rcp.noRef", "aucune référence"));
        el.textContent = s;
        const clr = $("#ptz-rcp-clearref");
        if (clr) clr.disabled = !rcpRef;
        const exp = $("#ptz-rcp-exportcsv");
        if (exp) exp.disabled = !rcpRef;                  // export réservé quand une référence existe
    }

    const fmtDate2 = (iso) => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); };

    // ── Export CSV de la référence ───────────────────────────
    // Produit un CSV du SNAPSHOT mémorisé, présenté comme le tableau RCP : 1ʳᵉ colonne =
    // libellé du réglage, puis une colonne par caméra ; une ligne d'en-tête par section
    // (groupe) ; une ligne par réglage. La structure lignes/colonnes reprend EXACTEMENT
    // `rcpBuildGroups` (donc l'ordre affiché). Cellule vide = pas de valeur pour cette caméra.
    // Séparateur « ; » (usage FR/Excel), BOM UTF-8, valeurs à risque entre guillemets doublés.
    function rcpCsvCell(s) {
        s = (s === null || s === undefined) ? "" : String(s);
        return /[;"\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }
    function rcpCsvCamHead(c) {
        const num = c.cam_number != null ? "N°" + c.cam_number : "";
        const model = (c.identity && c.identity.model) || c.model || "";
        return [num, c.name, model].filter(Boolean).join(" ");
    }
    // Valeur de référence formatée : enum → libellé de l'option (pas la valeur brute) ; sinon
    // valeur telle quelle. Renvoie "" si aucune référence pour ce couple (caméra, réglage).
    function rcpCsvRefFmt(p, v) {
        if (v === null || v === undefined) return "";
        if (p && p.type === "enum" && Array.isArray(p.options)) {
            const o = p.options.find((o) => o.value === v);
            if (o) return o.label;
        }
        return String(v);
    }
    // Texte de référence d'une cellule (comme à l'écran) : simple → une valeur ; trio → les
    // canaux présents côte à côte (« R=12 V=0 B=-3 »), dans l'ordre R·V·B.
    function rcpCsvRefCell(row, c) {
        if (row.kind === "triplet") {
            const dd = rcpData[c.id];
            if (!dd) return "";
            const members = Object.keys(dd.byKey).map((k) => dd.byKey[k])
                .filter((p) => p.triplet === row.triplet && (p.group || "") === row.group);
            members.sort((a, b) => rcpChanOrder(a.channel) - rcpChanOrder(b.channel));
            const parts = [];
            members.forEach((p) => {
                const v = rcpRefVal(c.id, p.key);
                if (v === undefined || v === null) return;
                const L = RCP_CH[p.channel] ? RCP_CH[p.channel].L : (p.channel || "");
                parts.push((L ? L + "=" : "") + rcpCsvRefFmt(p, v));
            });
            return parts.join(" ");
        }
        const dd = rcpData[c.id], p = dd && dd.byKey[row.key];
        return rcpCsvRefFmt(p, rcpRefVal(c.id, row.key));
    }
    function rcpExportCsv() {
        if (!rcpRef) return;
        const list = rcpCams();
        const groups = rcpBuildGroups(list);
        const ncol = list.length;
        const lines = [];
        lines.push([rcpCsvCell(tr("plugin.ptz.rcp.setting", "Réglage"))]
            .concat(list.map((c) => rcpCsvCell(rcpCsvCamHead(c)))).join(";"));
        groups.forEach((G) => {
            if (!G.rows.length) return;
            if (G.name) {                                 // ligne d'en-tête de section
                const pad = []; for (let i = 0; i < ncol; i++) pad.push("");
                lines.push([rcpCsvCell(G.name)].concat(pad).join(";"));
            }
            G.rows.forEach((row) => {
                lines.push([rcpCsvCell(row.label)]
                    .concat(list.map((c) => rcpCsvCell(rcpCsvRefCell(row, c)))).join(";"));
            });
        });
        const d = rcpRefDate ? new Date(rcpRefDate) : new Date();
        const stamp = isNaN(d) ? "" : d.toISOString().slice(0, 10);
        const csv = "\uFEFF" + lines.join("\r\n");        // BOM UTF-8 pour Excel
        try {
            const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url; a.download = "rcp-reference-" + stamp + ".csv";
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 0);
            toast(tr("plugin.ptz.rcp.csvExported", "Référence exportée en CSV"), "info");
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Noms des mémoires (matrice) ──────────────────────────
    // Mémoires en LIGNES, caméras en COLONNES. Même principe : squelette d'abord, puis
    // chaque caméra remplit sa colonne. Les cases restent désactivées tant que la valeur
    // n'est pas connue — on ne propose pas d'éditer un nom qu'on n'a pas encore lu.
    let nmData = {};                   // { camId: {on_device, lo, hi, names} }
    let nmErr = {};                    // { camId: message }

    function loadNames() {
        nmData = {}; nmErr = {};
        renderNames();
        cams.forEach((c) => {
            ctx.api("cameras/" + c.id + "/names")
                .then((d) => { nmData[c.id] = d; fillNamesColumn(c.id); })
                .catch((e) => { nmErr[c.id] = e.message; fillNamesColumn(c.id); });
        });
    }

    function nmRange() {
        let lo = 1, hi = 1;
        cams.forEach((c) => {
            const r = c.preset_range || [1, 100];
            lo = Math.min(lo, r[0]); hi = Math.max(hi, r[1]);
        });
        return [lo, hi];
    }

    function renderNames() {
        const body = $("#ptz-nm-body");
        if (!cams.length) {
            body.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.none",
                "Aucune caméra — utilisez « + Ajouter une caméra »."))}</div>`;
            return;
        }
        const [lo, hi] = nmRange();
        let from = Math.max(lo, parseInt($("#ptz-nm-from").value, 10) || lo);
        let to = Math.min(hi, parseInt($("#ptz-nm-to").value, 10) || hi);
        if (to < from) to = from;
        let idx = [];
        for (let i = from; i <= to; i++) idx.push(i);
        if ($("#ptz-nm-named").checked) {
            // Une mémoire n'est gardée que si AU MOINS une caméra y a un nom : sinon le
            // filtre masquerait les cases vides qu'on veut justement remplir.
            idx = idx.filter((i) => cams.some((c) => ((nmData[c.id] || {}).names || {})[i]));
            if (!idx.length) {
                body.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.noNamed",
                    "Aucune mémoire nommée."))}</div>`;
                updateNmMeta();
                return;
            }
        }
        const ordered = sortedCams().filter(isNumbered);
        body.innerHTML = `<div class="ptz-ov-scroll"><table class="ptz-tbl ptz-nm-tbl">
            <thead><tr>
              <th class="ptz-nm-idx">${esc(tr("plugin.ptz.nm.preset", "Mémoire"))}</th>
              ${ordered.map(camHead).join("")}
            </tr></thead>
            <tbody>${idx.map((i) => `<tr>
                <td class="ptz-nm-idx">${i}</td>
                ${ordered.map((c) => nameCell(c, i)).join("")}
              </tr>`).join("")}</tbody>
          </table></div>
          <p class="ptz-meta" style="margin-top:8px">${esc(tr("plugin.ptz.nm.help",
            "Saisissez un nom puis quittez la case pour l'enregistrer. Une case sur fond " +
            "bleuté est écrite DANS la caméra ; sinon le nom est tenu par l'outil. Vider " +
            "une case rétablit le libellé d'usine."))}</p>`;
        body.querySelectorAll(".ptz-nm-in").forEach((el) =>
            el.addEventListener("change", () => saveName(el)));
        applyStatusDots();
        updateNmMeta();
    }

    function camHead(c) {
        const d = nmData[c.id], err = nmErr[c.id];
        const tag = err
            ? `<span class="fail" title="${esc(err)}">⚠ ${esc(tr("plugin.ptz.unreachable", "injoignable"))}</span>`
            : !d ? `<span class="ptz-meta">…</span>`
            : d.on_device
                ? `<span class="ptz-tag" title="${esc(tr("plugin.ptz.nm.onDev",
                    "Les noms sont écrits dans la caméra"))}">${esc(tr("plugin.ptz.nm.dev", "caméra"))}</span>`
                : `<span class="ptz-tag" title="${esc(tr("plugin.ptz.nm.onLoc",
                    "Ce modèle ne mémorise pas de nom : tenu par l'outil"))}">${esc(tr("plugin.ptz.nm.loc", "outil"))}</span>`;
        const num = c.cam_number != null ? `<span class="ptz-num">N°${esc(c.cam_number)}</span> ` : "";
        return `<th class="ptz-nm-head" data-col-for="${esc(c.id)}">
            <div class="ptz-nm-headname"><span class="ptz-dot" data-dot-for="${esc(c.id)}"></span>
              ${num}${esc(c.name)}</div>
            <div data-head-tag>${tag}</div>
          </th>`;
    }

    function nameCell(c, i) {
        const d = nmData[c.id];
        if (nmErr[c.id]) return `<td class="ptz-out-na"></td>`;
        const r = c.preset_range || [1, 100];
        if (i < r[0] || i > r[1]) return `<td class="ptz-out-na"></td>`;
        if (!d) {
            return `<td><input class="ptz-nm-in" data-cam="${esc(c.id)}" data-idx="${i}"
                placeholder="…" disabled></td>`;
        }
        return `<td><input class="ptz-nm-in ${d.on_device ? "dev" : ""}"
            data-cam="${esc(c.id)}" data-idx="${i}" value="${esc(d.names[i] || "")}"
            maxlength="${d.on_device ? 15 : 64}" spellcheck="false"></td>`;
    }

    // Remplit UNE colonne sans reconstruire le tableau : reconstruire ferait perdre le
    // focus et la saisie en cours dans les autres colonnes.
    function fillNamesColumn(id) {
        if (!root) return;
        const c = cam(id);
        const th = root.querySelector(`[data-col-for="${id}"] [data-head-tag]`);
        if (th && c) th.innerHTML = camHead(c).match(/<div data-head-tag>([\s\S]*?)<\/div>/)[1];
        const d = nmData[id], err = nmErr[id];
        root.querySelectorAll(`.ptz-nm-in[data-cam="${id}"]`).forEach((el) => {
            const i = el.dataset.idx;
            if (err) { el.disabled = true; el.value = ""; el.placeholder = "—"; return; }
            if (!d) return;
            el.disabled = false;
            el.placeholder = "";
            el.value = d.names[i] || "";
            el.maxLength = d.on_device ? 15 : 64;
            el.classList.toggle("dev", !!d.on_device);
        });
        updateNmMeta();
    }

    function updateNmMeta() {
        const ok = Object.keys(nmData).length, ko = Object.keys(nmErr).length;
        const el = $("#ptz-nm-meta");
        if (!el) return;
        el.textContent = (ok + ko < cams.length)
            ? `${ok + ko}/${cams.length} ${tr("plugin.ptz.read", "lue(s)")}…`
            : `${ok} ${tr("plugin.ptz.read", "lue(s)")}` +
              (ko ? ` · ${ko} ${tr("plugin.ptz.failed", "échec(s)")}` : "");
    }

    async function saveName(el) {
        const camId = el.dataset.cam, idx = parseInt(el.dataset.idx, 10);
        const wanted = el.value;
        el.disabled = true;
        try {
            const r = await ctx.api("cameras/" + camId + "/presets/name",
                                    { body: { index: idx, name: wanted } });
            // La caméra peut n'accepter qu'une forme adaptée : on réaffiche CE QU'ELLE A
            // retenu, pas la saisie, et on le signale plutôt que de le changer en silence.
            el.value = r.name != null ? r.name : wanted;
            if (nmData[camId]) nmData[camId].names[idx] = el.value;
            if (r.changed) {
                toast(`${tr("plugin.ptz.nm.adapted", "Nom adapté par la caméra :")} ${el.value}`, "info");
            }
        } catch (e) {
            toast(e.message, "error");
            el.value = ((nmData[camId] || {}).names || {})[idx] || "";
        } finally { el.disabled = false; }
    }

    // ── Sauvegardes (instantanés) ────────────────────────────
    // Un instantané couvre une SÉLECTION de caméras — tout le parc par défaut. Il relève
    // TOUT ce qui est lisible, y compris ce qu'on ne sait pas réécrire : la comparaison
    // reste alors complète là où la restauration ne l'est pas.
    async function loadSnaps() {
        $("#ptz-snap-scope").textContent = snapScope();
        let list = [];
        try { list = (await ctx.api("snapshots")).snapshots || []; }
        catch (e) { toast(e.message, "error"); return; }
        const box = $("#ptz-snap-list");
        if (!list.length) {
            box.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.noSnap", "Aucun instantané."))}</div>`;
            return;
        }
        box.innerHTML = `<table class="ptz-tbl">
            <thead><tr><th>${esc(tr("plugin.ptz.snapName", "Instantané"))}</th>
              <th>${esc(tr("plugin.ptz.snapDate", "Date"))}</th>
              <th>${esc(tr("plugin.ptz.snapCams", "Caméras"))}</th><th></th></tr></thead>
            <tbody>${list.map((s) => `<tr>
                <td>${esc(s.name)}</td>
                <td class="ptz-meta">${esc(fmtDate(s.created_at))}</td>
                <td>${s.count}</td>
                <td class="ptz-snap-act">
                  <button class="btn" data-cmp="${esc(s.id)}" type="button">${esc(tr("plugin.ptz.compare", "Comparer"))}</button>
                  <button class="btn btn-icon btn-red" data-del="${esc(s.id)}" type="button" title="Supprimer">✕</button>
                </td></tr>`).join("")}</tbody></table>`;
        box.querySelectorAll("[data-cmp]").forEach((b) =>
            b.addEventListener("click", () => compareSnap(b.dataset.cmp)));
        box.querySelectorAll("[data-del]").forEach((b) =>
            b.addEventListener("click", () => deleteSnap(b.dataset.del)));
    }

    const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "";

    async function newSnap() {
        const s = (($("#ptz-snap-scopesel") || {}).value) || "presta";
        let ids = null, scope;
        if (s === "all") {
            scope = tr("plugin.ptz.snap.all", "tout le parc");
        } else {
            ids = cams.filter(isNumbered).map((c) => c.id);
            scope = tr("plugin.ptz.snap.presta", "caméras de la presta");
            if (!ids.length) { toast(tr("plugin.ptz.snap.noneNum", "Aucune caméra numérotée"), "error"); return; }
        }
        const name = prompt(`${tr("plugin.ptz.snapPrompt", "Nom de l'instantané")} (${scope}) :`,
                            tr("plugin.ptz.snapDefault", "Config de référence"));
        if (name === null) return;
        const body = { name };
        if (ids) body.ids = ids;
        try {
            const r = await ctx.api("snapshots", { body });
            toast(`${r.count_ok} ${tr("plugin.ptz.captured", "caméra(s) relevée(s)")}` +
                  (r.count_fail ? ` · ${r.count_fail} ${tr("plugin.ptz.failed", "échec(s)")}` : ""),
                  r.count_fail ? "error" : "info");
            loadSnaps();
        } catch (e) { toast(e.message, "error"); }
    }

    async function deleteSnap(sid) {
        if (!confirm(tr("plugin.ptz.confirmDelSnap", "Supprimer cet instantané ?"))) return;
        try {
            await ctx.api("snapshots/" + sid, { method: "DELETE" });
            $("#ptz-snap-diff").innerHTML = "";
            loadSnaps();
        } catch (e) { toast(e.message, "error"); }
    }

    async function compareSnap(sid) {
        const box = $("#ptz-snap-diff");
        box.innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.comparing", "Comparaison en cours…"))}</div>`;
        let d;
        try { d = await ctx.api("snapshots/" + sid + "/compare"); }
        catch (e) { box.innerHTML = `<div class="ptz-meta">${esc(e.message)}</div>`; return; }
        const t = d.tally || {};
        const rows = [];
        (d.cameras || []).forEach((c) => {
            if (!c.present) {
                rows.push(`<tr class="ptz-gone"><td>${esc(c.name)}</td><td colspan="5">${
                    esc(c.error || tr("plugin.ptz.camGone", "caméra absente du parc"))}</td></tr>`);
                return;
            }
            (c.params || []).forEach((p) => {
                const cls = p.status === "diff" ? "fail" : p.status === "same" ? "pass" : "";
                // Seules les lignes qui ONT dérivé et qu'on sait réécrire sont cochables.
                // Cocher par défaut ce qui a dérivé : c'est l'intention normale, mais rien
                // n'est envoyé sans validation explicite du bouton.
                const box_ = p.restorable && p.status === "diff"
                    ? `<input type="checkbox" class="ptz-rst" data-cam="${esc(c.id)}" data-key="${esc(p.key)}" checked>`
                    : `<span class="ptz-null" title="${esc(p.restorable
                        ? tr("plugin.ptz.noDrift", "identique — rien à renvoyer")
                        : tr("plugin.ptz.notRestorable", "non restaurable : commande d'écriture inconnue"))}">—</span>`;
                rows.push(`<tr>
                    <td>${esc(c.name)}</td>
                    <td>${esc(p.label)}${p.heavy ? ` <span class="ptz-unval" title="${
                        esc(tr("plugin.ptz.heavyHelp", "Écriture lourde : la caméra redémarre."))}">⏻</span>` : ""}</td>
                    <td>${esc(fmtVal(p.saved))}</td>
                    <td class="${cls}">${esc(fmtVal(p.current))}</td>
                    <td>${esc(statusLabel(p.status))}</td>
                    <td class="ptz-c-toggle">${box_}</td>
                  </tr>`);
            });
        });
        box.innerHTML = `
          <div class="ptz-snap-h">
            <strong>${esc(d.snapshot.name)}</strong>
            <span class="ptz-meta">${esc(fmtDate(d.snapshot.created_at))}</span>
            <span class="ptz-spacer"></span>
            <span class="ptz-meta">${t.same || 0} ${esc(tr("plugin.ptz.identical", "identiques"))}
              · ${t.diff || 0} ${esc(tr("plugin.ptz.drifted", "écart(s)"))}
              · ${t.restorable || 0} ${esc(tr("plugin.ptz.restorableN", "restaurable(s)"))}</span>
            <button class="btn btn-green" id="ptz-do-restore" type="button" ${t.restorable ? "" : "disabled"}>${
              esc(tr("plugin.ptz.restore", "Restaurer la sélection"))}</button>
          </div>
          <table class="ptz-tbl">
            <thead><tr>
              <th>${esc(tr("plugin.ptz.camera", "Caméra"))}</th>
              <th>${esc(tr("plugin.ptz.param", "Paramètre"))}</th>
              <th>${esc(tr("plugin.ptz.colSaved", "Enregistré"))}</th>
              <th>${esc(tr("plugin.ptz.current", "Actuel"))}</th>
              <th>${esc(tr("plugin.ptz.state", "État"))}</th>
              <th class="ptz-c-toggle">${esc(tr("plugin.ptz.send", "Renvoyer"))}</th>
            </tr></thead>
            <tbody>${rows.join("")}</tbody>
          </table>`;
        const btn = $("#ptz-do-restore");
        if (btn) btn.addEventListener("click", () => doRestore(sid));
    }

    const fmtVal = (v) => v === null || v === undefined ? tr("plugin.ptz.notRead", "non lu")
        : v === true ? tr("plugin.ptz.yes", "Activé") : v === false ? tr("plugin.ptz.no", "Désactivé") : String(v);

    function statusLabel(s) {
        return { same: tr("plugin.ptz.stSame", "identique"),
                 diff: tr("plugin.ptz.stDiff", "a changé"),
                 unread: tr("plugin.ptz.stUnread", "illisible"),
                 unsaved: tr("plugin.ptz.stUnsaved", "non enregistré") }[s] || s;
    }

    async function doRestore(sid) {
        const picked = {};
        root.querySelectorAll(".ptz-rst:checked").forEach((cb) =>
            (picked[cb.dataset.cam] = picked[cb.dataset.cam] || []).push(cb.dataset.key));
        const items = Object.keys(picked).map((cam) => ({ camera_id: cam, keys: picked[cam] }));
        const n = items.reduce((s, i) => s + i.keys.length, 0);
        if (!n) { toast(tr("plugin.ptz.nothingPicked", "Rien de sélectionné"), "error"); return; }
        const heavy = root.querySelectorAll(".ptz-rst:checked").length &&
            [...root.querySelectorAll(".ptz-rst:checked")].some((cb) =>
                cb.closest("tr").querySelector(".ptz-unval"));
        let msg = `${tr("plugin.ptz.confirmRestore", "Renvoyer")} ${n} ` +
                  `${tr("plugin.ptz.paramsOn", "paramètre(s) sur")} ${items.length} ` +
                  `${tr("plugin.ptz.cameras", "caméra(s)")} ?`;
        if (heavy) msg += "\n\n⚠ " + tr("plugin.ptz.heavyWarn",
            "La sélection contient un réglage qui fait REDÉMARRER la caméra (~2 min).");
        if (!confirm(msg)) return;
        try {
            const r = await ctx.api("snapshots/" + sid + "/restore", { body: { items } });
            renderReport(tr("plugin.ptz.restore", "Restaurer la sélection"), r);
            toast(`${r.count_ok} ${tr("plugin.ptz.done", "OK")}` +
                  (r.count_fail ? ` · ${r.count_fail} ${tr("plugin.ptz.failed", "échec(s)")}` : ""),
                  r.count_fail ? "error" : "info");
            compareSnap(sid);
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Actions groupées ─────────────────────────────────────
    function renderBulkBar() {
        const bar = $("#ptz-bulk");
        bar.hidden = selected.size === 0;
        if (bar.hidden) return;
        $("#ptz-bulk-n").textContent =
            `${selected.size} ${tr("plugin.ptz.selectedN", "caméra(s) sélectionnée(s)")}`;
        fillBulkKeys();
    }

    function bulkCams() { return cams.filter((c) => selected.has(c.id)); }

    async function fillBulkKeys() {
        const sel = $("#ptz-bulk-key");
        const first = bulkCams()[0];
        if (!first) return;
        // Le schéma dépend du modèle : on prend celui de la 1re caméra sélectionnée et on
        // signale si la sélection mélange plusieurs pilotes (les paramètres peuvent différer).
        let schema = schemaCache[first.id];
        if (!schema) {
            try {
                const d = await ctx.api("cameras/" + first.id + "/params?keys=__none__");
                schema = d.schema || [];
                schemaCache[first.id] = schema;
            } catch (e) { schema = []; }
        }
        const bulkable = schema.filter((p) => p.bulk && p.writable);
        const prev = sel.value;
        sel.innerHTML = bulkable.map((p) =>
            `<option value="${esc(p.key)}">${esc(p.label)}${p.validated ? "" : " ⚠"}</option>`).join("");
        if (prev && bulkable.some((p) => p.key === prev)) sel.value = prev;
        renderBulkValue();
    }

    function bulkSchema() {
        const first = bulkCams()[0];
        return (first && schemaCache[first.id]) || [];
    }

    function renderBulkValue() {
        const key = $("#ptz-bulk-key").value;
        const p = bulkSchema().find((x) => x.key === key);
        const box = $("#ptz-bulk-valbox");
        if (!p) { box.innerHTML = ""; return; }
        if (p.type === "bool") {
            box.innerHTML = `<select id="ptz-bulk-val"><option value="1">${esc(tr("plugin.ptz.yes", "Activé"))}</option>
                <option value="0">${esc(tr("plugin.ptz.no", "Désactivé"))}</option></select>`;
        } else if (p.type === "enum") {
            box.innerHTML = `<select id="ptz-bulk-val">${(p.options || []).map((o) =>
                `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}</select>`;
        } else {
            box.innerHTML = `<input type="${p.type === "int" ? "number" : "text"}" id="ptz-bulk-val">`;
        }
    }

    async function bulkParams() {
        const key = $("#ptz-bulk-key").value;
        const p = bulkSchema().find((x) => x.key === key);
        const el = $("#ptz-bulk-val");
        if (!p || !el) { toast(tr("plugin.ptz.noBulkParam", "Aucun paramètre applicable"), "error"); return; }
        const raw = el.value;
        const val = p.type === "bool" ? raw === "1" : p.type === "int" ? parseInt(raw, 10) : raw;
        const n = selected.size;
        if (!confirm(`${tr("plugin.ptz.confirmBulk", "Appliquer")} « ${p.label} = ${raw} » ` +
                     `${tr("plugin.ptz.onN", "sur")} ${n} ${tr("plugin.ptz.cameras", "caméra(s)")} ?`)) return;
        await runBulk("params", { ids: [...selected], values: { [key]: val } },
                      `${p.label} = ${raw}`);
    }

    async function bulkPreset(mode) {
        const index = parseInt($("#ptz-bulk-preset").value, 10);
        if (!index) { toast(tr("plugin.ptz.presetRequired", "Numéro de mémoire requis"), "error"); return; }
        if (!confirm(`${tr("plugin.ptz.confirmRecall", "Rappeler la mémoire")} ${index} ` +
                     `${tr("plugin.ptz.onN", "sur")} ${selected.size} ${tr("plugin.ptz.cameras", "caméra(s)")} ?`)) return;
        await runBulk("preset", { ids: [...selected], index, mode },
                      `${tr("plugin.ptz.bulk.preset", "Mémoire")} ${index}`);
    }

    async function bulkRename() {
        const index = parseInt($("#ptz-bulk-preset").value, 10);
        if (!index) { toast(tr("plugin.ptz.presetRequired", "Numéro de mémoire requis"), "error"); return; }
        const name = prompt(`${tr("plugin.ptz.renamePrompt", "Nom de la mémoire")} ${index} ` +
                            `(${selected.size} ${tr("plugin.ptz.cameras", "caméra(s)")}) :`, "");
        if (name === null) return;
        await runBulk("name", { ids: [...selected], index, name },
                      `${tr("plugin.ptz.bulk.rename", "Renommer")} ${index}`);
        if (tab === "presets" && selected.has(selId)) renderDetail();
    }

    async function bulkIdentify() {
        await runBulk("identify", { ids: [...selected] }, tr("plugin.ptz.bulk.identify", "Détecter les modèles"));
        refresh();
    }

    async function runBulk(action, body, title) {
        try {
            const r = await ctx.api("bulk/" + action, { body });
            renderReport(title, r);
            toast(`${r.count_ok} ${tr("plugin.ptz.done", "OK")}` +
                  (r.count_fail ? ` · ${r.count_fail} ${tr("plugin.ptz.failed", "échec(s)")}` : ""),
                  r.count_fail ? "error" : "info");
        } catch (e) { toast(e.message, "error"); }
    }

    // Compte-rendu ligne à ligne : une action groupée réussit presque toujours
    // PARTIELLEMENT, un verdict global masquerait les caméras restées en arrière.
    function renderReport(title, r) {
        const box = $("#ptz-report");
        box.hidden = false;
        const rows = (r.results || []).filter(Boolean);
        box.innerHTML = `
          <div class="ptz-report-h">
            <strong>${esc(title)}</strong>
            <span class="ptz-meta">${r.count_ok} ${esc(tr("plugin.ptz.done", "OK"))}
              ${r.count_fail ? "· " + r.count_fail + " " + esc(tr("plugin.ptz.failed", "échec(s)")) : ""}</span>
            <span class="ptz-spacer"></span>
            <button class="btn btn-icon" id="ptz-report-x" type="button" title="Fermer">✕</button>
          </div>
          <table class="ptz-tbl">
            <thead><tr><th>${esc(tr("plugin.ptz.camera", "Caméra"))}</th>
              <th>${esc(tr("plugin.ptz.result", "Résultat"))}</th></tr></thead>
            <tbody>${rows.map((x) => `<tr>
                <td>${esc(x.name)}</td>
                <td class="${x.ok ? "pass" : "fail"}">${esc(x.ok ? (x.detail || "OK") : x.error)}</td>
              </tr>`).join("")}</tbody>
          </table>`;
        $("#ptz-report-x").addEventListener("click", () => { box.hidden = true; });
    }

    // ── Pupitres ─────────────────────────────────────────────
    // Un pupitre tient une table d'affectation (numéro caméra AU pupitre → caméra réelle).
    // Écrire, c'est de la config d'exploitation : chaque envoi est confirmé, et on n'envoie
    // QUE les slots réellement changés (le pilote préserve le reste, mots de passe compris).
    let pnls = [];                     // parc de pupitres
    let pnlCatalog = [];               // pilotes de pupitres disponibles
    let selPanel = null;               // pupitre affiché
    let assign = null;                 // { assignments, cameras, slot_count } du pupitre choisi
    let pending = {};                  // slot(str) → { camera_id } | { control_type:"3" }
    let pnAssignedOnly = false;
    let pnMode = "assign";             // sous-vue du pupitre : "assign" | "macros"
    let macroSel = 1;                  // n° de macro affichée
    let macroSteps = [];               // copie de travail des étapes (éditée avant Enregistrer)
    let macroInfo = { max_steps: 500, macro_count: 100 };

    async function loadPanels() {
        const box = $("#ptz-pn-list");
        try {
            const [ps, ds] = await Promise.all([ctx.api("panels"), ctx.api("panels/drivers")]);
            pnls = (ps && ps.panels) || [];
            pnlCatalog = (ds && ds.drivers) || [];
        } catch (e) { toast(e.message, "error"); return; }
        renderPanelList();
        if (selPanel && !pnls.find((p) => p.id === selPanel)) {
            selPanel = null; assign = null; pending = {};
            $("#ptz-pn-assign").innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.pn.pick",
                "Choisissez un pupitre pour voir ses affectations."))}</div>`;
        }
    }

    function renderPanelList() {
        const box = $("#ptz-pn-list");
        if (!pnls.length) {
            box.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.pn.none",
                "Aucun pupitre — utilisez « + Ajouter un pupitre »."))}</div>`;
            return;
        }
        box.innerHTML = pnls.map((p) => {
            const dot = p.reachable === true ? "ok" : p.reachable === false ? "err" : "";
            const sub = p.driver_ok
                ? `${esc(p.host)} · ${p.slot_count || "?"} ${esc(tr("plugin.ptz.pn.slots", "slots"))}`
                : `<span class="fail">${esc(p.driver_error || "")}</span>`;
            return `<div class="ptz-pn-card${p.id === selPanel ? " on" : ""}" data-id="${esc(p.id)}">
                <div class="ptz-pn-card-h">
                  <span class="ptz-dot ${dot}" title="${esc(p.error || "")}"></span>
                  <strong>${esc(p.name || p.host)}</strong>
                  <span class="ptz-spacer"></span>
                  <button class="btn btn-icon" data-a="web" type="button" title="${esc(tr("plugin.ptz.webPageTip", "Ouvrir l'interface web dans un nouvel onglet"))}">↗</button>
                  <button class="btn btn-icon" data-a="edit" type="button" title="${esc(tr("plugin.ptz.edit", "Modifier"))}">✎</button>
                  <button class="btn btn-icon" data-a="del" type="button" title="${esc(tr("plugin.ptz.delete", "Supprimer"))}">🗑</button>
                </div>
                <div class="ptz-meta">${sub}</div>
              </div>`;
        }).join("");
        box.querySelectorAll(".ptz-pn-card").forEach((el) => {
            const id = el.dataset.id;
            el.addEventListener("click", (e) => {
                const act = e.target.closest("[data-a]");
                if (act) { e.stopPropagation(); return panelCardAction(act.dataset.a, id); }
                selectPanel(id);
            });
        });
    }

    function panelCardAction(a, id) {
        const p = pnls.find((x) => x.id === id);
        if (a === "web") return openWebPage(p && p.host);
        if (a === "edit") return showPanelForm(p);
        if (a === "del") {
            if (!window.confirm(tr("plugin.ptz.pn.delConfirm", "Retirer ce pupitre du parc ?"))) return;
            ctx.api("panels/" + id, { method: "DELETE" })
                .then(() => { toast(tr("plugin.ptz.pn.deleted", "Pupitre retiré"), "success"); loadPanels(); })
                .catch((e) => toast(e.message, "error"));
        }
    }

    // -- formulaire pupitre --
    let pnEditId = null;

    function fillPanelDrivers(sel, chosen) {
        sel.innerHTML = pnlCatalog.map((d) =>
            `<option value="${esc(d.kind)}"${d.kind === chosen ? " selected" : ""}>${esc(d.label)}${d.available ? "" : " (indispo.)"}</option>`).join("");
        fillPanelModels(chosen || (pnlCatalog[0] && pnlCatalog[0].kind));
    }

    function fillPanelModels(kind, chosen) {
        const d = pnlCatalog.find((x) => x.kind === kind);
        const sel = $("#ptz-pn-model");
        sel.innerHTML = ((d && d.models) || [{ value: "", label: "Auto" }]).map((m) =>
            `<option value="${esc(m.value)}"${m.value === (chosen || "") ? " selected" : ""}>${esc(m.label)}</option>`).join("");
    }

    function showPanelForm(p) {
        pnEditId = p ? p.id : null;
        const f = $("#ptz-pn-form");
        f.hidden = false;
        fillPanelDrivers($("#ptz-pn-driver"), p ? p.driver : undefined);
        $("#ptz-pn-driver").onchange = () => fillPanelModels($("#ptz-pn-driver").value);
        fillPanelModels(p ? p.driver : ($("#ptz-pn-driver").value), p ? p.model : "");
        $("#ptz-pn-name").value = p ? (p.name || "") : "";
        $("#ptz-pn-host").value = p ? (p.host || "") : "";
        $("#ptz-pn-port").value = p && p.port ? p.port : "";
        $("#ptz-pn-user").value = p ? (p.user || "") : "";
        $("#ptz-pn-pass").value = "";
        $("#ptz-pn-pass").placeholder = p && p.has_password
            ? tr("plugin.ptz.unchanged", "(inchangé)") : "";
        const note = pnlCatalog.find((d) => d.kind === $("#ptz-pn-driver").value);
        $("#ptz-pn-form-note").textContent = (note && note.note) || "";
    }

    function hidePanelForm() { $("#ptz-pn-form").hidden = true; pnEditId = null; }

    async function onPanelSubmit(e) {
        e.preventDefault();
        const body = {
            name: $("#ptz-pn-name").value.trim(),
            driver: $("#ptz-pn-driver").value,
            host: $("#ptz-pn-host").value.trim(),
            port: $("#ptz-pn-port").value ? parseInt($("#ptz-pn-port").value, 10) : "",
            model: $("#ptz-pn-model").value,
            user: $("#ptz-pn-user").value.trim(),
        };
        const pw = $("#ptz-pn-pass").value;
        if (pw) body.password = pw;            // vide = ne change rien (jamais renvoyé au front)
        if (!body.host) { toast(tr("plugin.ptz.f.hostReq", "Adresse requise"), "error"); return; }
        try {
            if (pnEditId) await ctx.api("panels/" + pnEditId, { method: "PUT", body });
            else await ctx.api("panels", { body });
            hidePanelForm();
            toast(tr("plugin.ptz.saved", "Enregistré"), "success");
            await loadPanels();
        } catch (err) { toast(err.message, "error"); }
    }

    // -- sélection & affectations --
    async function selectPanel(id) {
        selPanel = id; pending = {}; assign = null;
        renderPanelList();
        renderPanelPane();
        loadPnMode();
    }

    // Coquille du panneau de droite : une bascule Affectations / Macros, puis un conteneur
    // que chaque sous-vue remplit. La bascule ne recharge le pupitre que si nécessaire.
    function renderPanelPane() {
        const box = $("#ptz-pn-assign");
        const tab = (m, label) => `<button class="ptz-pn-modebtn${pnMode === m ? " on" : ""}"
            data-mode="${m}" type="button">${esc(label)}</button>`;
        box.innerHTML = `
          <div class="ptz-pn-modes">
            ${tab("assign", tr("plugin.ptz.pn.tabAssign", "Affectations"))}
            ${tab("macros", tr("plugin.ptz.pn.tabMacros", "Macros"))}
          </div>
          <div id="ptz-pn-content"><div class="ptz-meta">${esc(tr("plugin.ptz.loading", "Chargement…"))}</div></div>`;
        box.querySelectorAll(".ptz-pn-modebtn").forEach((b) =>
            b.addEventListener("click", () => { if (pnMode !== b.dataset.mode) { pnMode = b.dataset.mode; renderPanelPane(); loadPnMode(); } }));
    }

    async function loadPnMode() {
        if (pnMode === "macros") {
            // Les étapes de macro désignent des SLOTS pupitre : sans la table d'affectation,
            // le sélecteur de caméra ne pourrait proposer que des numéros nus, sans dire à
            // quelle caméra ils correspondent. On la charge donc aussi dans ce mode, et son
            // échec ne bloque pas l'édition — la liste se dégrade alors en slots seuls.
            if (!assign) {
                try { assign = await ctx.api("panels/" + selPanel + "/assignments"); }
                catch (e) { assign = null; }
            }
            return loadMacro(macroSel);
        }
        // mode affectations
        try {
            assign = await ctx.api("panels/" + selPanel + "/assignments");
        } catch (e) {
            $("#ptz-pn-content").innerHTML = `<div class="ptz-empty fail">${esc(e.message)}</div>`;
            return;
        }
        renderAssign();
    }

    // Affichage d'un slot (en tenant compte d'un changement en attente) : { camId, ip }.
    // La caméra choisie et l'adresse IP sont LIÉES : choisir une caméra remplit l'IP ; taper
    // une IP connue du parc re-sélectionne la caméra ; une IP hors parc reste en manuel.
    function camHost(id) {
        const c = (assign.cameras || []).find((x) => x.id === id);
        return c ? c.host : "";
    }
    function slotDisplay(r) {
        const pend = pending[r.slot];
        if (pend) {
            if (pend.camera_id) return { camId: pend.camera_id, ip: camHost(pend.camera_id) };
            if (pend.control_type === "3") return { camId: "", ip: "", manual: false };
            return { camId: "", ip: pend.ip || "", manual: true };
        }
        if (r.camera) return { camId: r.camera.id, ip: r.ip || camHost(r.camera.id) };
        if (r.control_type === "1" && (r.ip || "").trim()) return { camId: "", ip: r.ip, manual: true };
        return { camId: "", ip: "", manual: false };
    }

    function renderAssign() {
        if (!assign) return;
        const box = $("#ptz-pn-content") || $("#ptz-pn-assign");
        const rows = assign.assignments || [];
        const cams = (assign.cameras || []).filter(isNumbered);
        const nbAssigned = rows.filter((r) => r.control_type === "1").length;
        const shown = pnAssignedOnly ? rows.filter((r) => r.control_type === "1") : rows;
        const camLabel = (c) => (c.cam_number != null ? "N°" + c.cam_number + " · " : "")
            + c.name + " · " + c.host;

        const cellHtml = (r) => {
            const d = slotDisplay(r);
            let opts = `<option value="none"${(!d.camId && !d.manual) ? " selected" : ""}>${esc(tr("plugin.ptz.pn.noassign", "— Non affecté —"))}</option>`;
            if (d.manual) {
                opts += `<option value="manual" selected>${esc(tr("plugin.ptz.pn.manualLabel", "Saisie manuelle"))}</option>`;
            }
            cams.forEach((c) => {
                opts += `<option value="cam:${esc(c.id)}"${d.camId === c.id ? " selected" : ""}>${esc(camLabel(c))}</option>`;
            });
            return `<td><select class="ptz-pn-sel" data-slot="${r.slot}">${opts}</select></td>
              <td><input class="ptz-pn-ip" data-slot="${r.slot}" placeholder="${esc(tr("plugin.ptz.pn.ip", "adresse IP"))}" value="${esc(d.ip)}">
                  <button class="btn btn-icon ptz-pn-validate" data-slot="${r.slot}" type="button" title="${esc(tr("plugin.ptz.pn.validateTip", "Valider l'adresse saisie"))}">✓</button></td>
              <td><button class="btn btn-icon btn-red ptz-pn-clear" data-slot="${r.slot}" type="button" title="${esc(tr("plugin.ptz.pn.clearOne", "Retirer l'affectation"))}">✕</button></td>`;
        };

        box.innerHTML = `
          <div class="ptz-pn-assign-h">
            <strong>${esc(selPanelName())}</strong>
            <span class="ptz-meta">${nbAssigned}/${rows.length} ${esc(tr("plugin.ptz.pn.assigned", "affectés"))}</span>
            <label class="ptz-selall"><input type="checkbox" id="ptz-pn-only"${pnAssignedOnly ? " checked" : ""}>
              <span>${esc(tr("plugin.ptz.pn.onlyAssigned", "N'afficher que les affectés"))}</span></label>
            <button class="btn" id="ptz-pn-prefill" type="button"
              title="${esc(tr("plugin.ptz.pn.prefillTip", "Affecter chaque slot C_n à la caméra du parc portant le numéro n"))}"
              >${esc(tr("plugin.ptz.pn.prefill", "Pré-remplir par numéro"))}</button>
            <button class="btn btn-red" id="ptz-pn-clearall" type="button"
              >${esc(tr("plugin.ptz.pn.clearAll", "Tout supprimer"))}</button>
            <span class="ptz-spacer"></span>
            <span class="ptz-meta" id="ptz-pn-pending"></span>
            <button class="btn btn-green" id="ptz-pn-apply" type="button" disabled
              data-i18n="plugin.ptz.pn.apply">Appliquer les changements</button>
          </div>
          <div class="ptz-pn-table-wrap">
          <table class="ptz-tbl ptz-pn-table">
            <thead><tr>
              <th>${esc(tr("plugin.ptz.pn.slot", "N°"))}</th>
              <th>${esc(tr("plugin.ptz.pn.type", "Type"))}</th>
              <th>${esc(tr("plugin.ptz.pn.target", "Caméra affectée"))}</th>
              <th>${esc(tr("plugin.ptz.pn.ip", "adresse IP"))}</th>
              <th></th>
            </tr></thead>
            <tbody>${shown.map((r) => `<tr data-slot="${r.slot}"${pending[r.slot] ? ' class="ptz-pn-changed"' : ""}>
              <td class="ptz-pn-slot">C${String(r.slot).padStart(3, "0")}</td>
              <td>${esc(r.control_label)}</td>
              ${cellHtml(r)}
            </tr>`).join("")}</tbody>
          </table></div>`;

        $("#ptz-pn-only").addEventListener("change", (e) => { pnAssignedOnly = e.target.checked; renderAssign(); });
        $("#ptz-pn-prefill").addEventListener("click", prefillByNumber);
        $("#ptz-pn-clearall").addEventListener("click", clearAllAssign);
        box.querySelectorAll(".ptz-pn-sel").forEach((sel) =>
            sel.addEventListener("change", () => onSlotChange(sel)));
        // L'IP se VALIDE explicitement (bouton ✓ ou Entrée) — on ne re-render pas au blur,
        // sinon le clic sur ✓ serait perdu (le champ perd le focus et la table se redessine).
        box.querySelectorAll(".ptz-pn-validate").forEach((b) =>
            b.addEventListener("click", () => onValidateIp(+b.dataset.slot)));
        box.querySelectorAll(".ptz-pn-ip").forEach((inp) =>
            inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); onValidateIp(+inp.dataset.slot); } }));
        box.querySelectorAll(".ptz-pn-clear").forEach((b) =>
            b.addEventListener("click", () => { pending[+b.dataset.slot] = { slot: +b.dataset.slot, control_type: "3" }; renderAssign(); }));
        $("#ptz-pn-apply").addEventListener("click", applyAssign);
        refreshPendingLabel();
    }

    const selPanelName = () => { const p = pnls.find((x) => x.id === selPanel); return p ? (p.name || p.host) : ""; };

    function onSlotChange(sel) {
        const slot = Number(sel.dataset.slot);
        const v = sel.value;
        if (v === "none") pending[slot] = { slot: slot, control_type: "3" };
        else if (v === "manual") {
            const ip = (($(`.ptz-pn-ip[data-slot="${slot}"]`) || {}).value || "").trim();
            pending[slot] = { slot: slot, control_type: "1", ip: ip, port: "80" };
        } else if (v.startsWith("cam:")) pending[slot] = { slot: slot, camera_id: v.slice(4) };
        renderAssign();      // met à jour la colonne IP (auto-remplie depuis la caméra)
    }

    // Valide l'IP saisie sur un slot : IP connue du parc → on relie la caméra ; IP inconnue →
    // on propose de l'AJOUTER au parc (puis on la sélectionne), sinon on reste en saisie
    // manuelle. Dans tous les cas la colonne de gauche cesse d'afficher « Non affecté ».
    async function onValidateIp(slot) {
        const inp = $(`.ptz-pn-ip[data-slot="${slot}"]`);
        const ip = ((inp && inp.value) || "").trim();
        if (!ip) { pending[slot] = { slot: slot, control_type: "3" }; renderAssign(); return; }
        const c = (assign.cameras || []).find((x) => (x.host || "").trim() === ip);
        if (c) { pending[slot] = { slot: slot, camera_id: c.id }; renderAssign(); return; }
        if (window.confirm(tr("plugin.ptz.pn.addCamConfirm",
            "Cette adresse n'est pas dans le parc. L'ajouter comme nouvelle caméra ?") + "\n" + ip)) {
            try {
                const res = await ctx.api("cameras", { body: { host: ip, name: ip } });
                // Recharge les affectations (la liste des caméras y est rafraîchie) SANS
                // toucher aux autres changements en attente, puis sélectionne la nouvelle.
                assign = await ctx.api("panels/" + selPanel + "/assignments");
                const newId = res && res.camera && res.camera.id;
                if (newId) pending[slot] = { slot: slot, camera_id: newId };
                toast(tr("plugin.ptz.pn.camAdded", "Caméra ajoutée au parc"), "success");
                invalidateViews();
                renderAssign();
                return;
            } catch (e) { toast(e.message, "error"); }
        }
        pending[slot] = { slot: slot, control_type: "1", ip: ip, port: "80" };  // reste manuel
        renderAssign();
    }

    function clearAllAssign() {
        if (!window.confirm(tr("plugin.ptz.pn.clearAllConfirm",
            "Retirer TOUTES les affectations de ce pupitre ?"))) return;
        (assign.assignments || []).forEach((r) => { pending[r.slot] = { slot: r.slot, control_type: "3" }; });
        renderAssign();
    }

    function prefillByNumber() {
        const byNum = {};
        (assign.cameras || []).forEach((c) => { if (c.cam_number != null) byNum[c.cam_number] = c; });
        let n = 0;
        (assign.assignments || []).forEach((r) => {
            const c = byNum[r.slot];
            if (c) { pending[r.slot] = { slot: r.slot, camera_id: c.id }; n++; }
        });
        renderAssign();
        toast(n ? `${n} ${tr("plugin.ptz.pn.prefilled", "slot(s) pré-remplis — vérifiez puis Appliquer")}`
                : tr("plugin.ptz.pn.prefillNone", "aucune caméra numérotée ne correspond à un slot"),
              n ? "info" : "error");
    }

    function refreshPendingLabel() {
        const n = Object.keys(pending).length;
        const lbl = $("#ptz-pn-pending"), btn = $("#ptz-pn-apply");
        if (lbl) lbl.textContent = n ? `${n} ${tr("plugin.ptz.pn.changes", "changement(s) en attente")}` : "";
        if (btn) btn.disabled = !n;
    }

    async function applyAssign() {
        const rows = Object.values(pending);
        if (!rows.length) return;
        const msg = tr("plugin.ptz.pn.applyConfirm",
            "Écrire ces affectations dans le pupitre ? C'est une modification d'exploitation.")
            + ` (${rows.length})`;
        if (!window.confirm(msg)) return;
        const btn = $("#ptz-pn-apply");
        btn.disabled = true;
        try {
            assign = await ctx.api("panels/" + selPanel + "/assignments", { body: { rows } });
            pending = {};
            toast(tr("plugin.ptz.pn.applied", "Affectations écrites"), "success");
            renderAssign();
        } catch (e) {
            toast(e.message, "error");
            btn.disabled = false;
        }
    }

    // ── Éditeur de macros ────────────────────────────────────
    // Une macro est une suite d'étapes typées. On édite une COPIE (macroSteps) ; rien ne part
    // au pupitre tant qu'on n'a pas cliqué « Enregistrer ». « Jouer » pilote les VRAIES
    // caméras → confirmation. Types offerts = ceux qui écrivent de façon fiable sur RP200.
    function macroTypes() {
        return [
            { v: "recall_preset", l: tr("plugin.ptz.mc.recallPreset", "Rappel mémoire"), cam: true, param: "num" },
            { v: "recall_macro", l: tr("plugin.ptz.mc.recallMacro", "Appeler une macro"), cam: false, param: "num" },
            { v: "wait_user", l: tr("plugin.ptz.mc.wait", "Attente utilisateur"), cam: false, param: "none" },
            { v: "aw_raw", l: tr("plugin.ptz.mc.awRaw", "Commande AW brute"), cam: true, param: "text" },
            { v: "cgi_get", l: tr("plugin.ptz.mc.cgiGet", "CGI (GET)"), cam: false, param: "text" },
            { v: "cgi_post", l: tr("plugin.ptz.mc.cgiPost", "CGI (POST)"), cam: false, param: "text" },
        ];
    }
    const macroTypeDef = (v) => macroTypes().find((t) => t.v === v) || macroTypes()[0];

    async function loadMacro(n) {
        macroSel = Number(n) || 1;
        const box = $("#ptz-pn-content");
        if (box) box.innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.loading", "Chargement…"))}</div>`;
        try {
            const d = await ctx.api("panels/" + selPanel + "/macros/" + macroSel);
            macroInfo = { max_steps: d.max_steps || 500, macro_count: d.macro_count || 100 };
            macroSteps = (d.steps || []).map((s) => ({
                type: s.type, cam: s.cam || "", param: s.param || "",
                interval: s.interval || 0, camera: s.camera || null,
            }));
        } catch (e) {
            if (box) box.innerHTML = `<div class="ptz-empty fail">${esc(e.message)}</div>`;
            return;
        }
        renderMacros();
    }

    // Caméra d'une étape de macro : la valeur écrite est le SLOT PUPITRE, jamais le numéro
    // d'exploitation.
    //
    // Ce sont deux numérotations distinctes, et les confondre déplaçait une autre caméra que
    // celle programmée — en direct, sans rien signaler. Le pupitre ne connaît que des slots ;
    // le serveur résout ensuite slot → IP (table d'affectation) → caméra du parc
    // (cf. server.py, `by_slot.get(str(s.get("cam")))`). La liste est donc peuplée depuis les
    // AFFECTATIONS du pupitre, pas depuis le parc, et affiche les deux repères pour que
    // l'opérateur reconnaisse sa caméra : « C007 · N°3 Plateau ».
    function macroCamSelect(i, cur) {
        let rows = ((assign && assign.assignments) || []).slice()
            .sort((a, b) => (a.slot || 0) - (b.slot || 0));
        // Table d'affectation indisponible (pupitre injoignable) : on propose les slots nus
        // plutôt qu'une liste vide, pour que l'édition reste possible — mais on ne prétend
        // pas savoir quelle caméra s'y trouve.
        if (!rows.length) {
            const n = (assign && assign.slot_count) || 24;
            rows = Array.from({ length: n }, (_, k) => ({ slot: k + 1 }));
        }
        let opts = `<option value="">—</option>`;
        let found = false;
        rows.forEach((r) => {
            if (!r.slot) return;
            const v = String(r.slot);
            const sel = String(cur || "") === v;
            if (sel) found = true;
            const c = r.camera;
            const num = c && c.cam_number ? ` · N°${esc(String(c.cam_number))}` : "";
            const nom = c ? ` ${esc(c.name)}` : (r.ip ? ` ${esc(r.ip)}` : " (slot libre)");
            opts += `<option value="${esc(v)}"${sel ? " selected" : ""}>C${String(r.slot).padStart(3, "0")}${num}${nom}</option>`;
        });
        // Valeur déjà enregistrée qui ne correspond à aucun slot connu : on la garde telle
        // quelle plutôt que de la perdre en silence, mais on la marque comme non résolue.
        if (cur && !found) {
            opts = `<option value="${esc(cur)}" selected>C${esc(String(cur))} · slot non affecté</option>` + opts;
        }
        return `<select class="ptz-mc-cam" data-idx="${i}">${opts}</select>`;
    }

    function macroStepRow(s, i) {
        const def = macroTypeDef(s.type);
        const typeSel = `<select class="ptz-mc-type" data-idx="${i}">${macroTypes().map((t) =>
            `<option value="${t.v}"${t.v === s.type ? " selected" : ""}>${esc(t.l)}</option>`).join("")}</select>`;
        const camCell = def.cam ? macroCamSelect(i, s.cam) : "—";
        let paramCell = "—";
        if (def.param === "num") {
            const max = s.type === "recall_macro" ? macroInfo.macro_count : 100;
            paramCell = `<input class="ptz-mc-param" type="number" min="1" max="${max}" data-idx="${i}" value="${esc(s.param)}">`;
        } else if (def.param === "text") {
            paramCell = `<input class="ptz-mc-param" type="text" data-idx="${i}" value="${esc(s.param)}" placeholder="${esc(tr("plugin.ptz.mc.cmd", "commande"))}">`;
        }
        return `<tr>
            <td class="ptz-pn-slot">${i + 1}</td>
            <td>${typeSel}</td>
            <td>${camCell}</td>
            <td>${paramCell}</td>
            <td><input class="ptz-mc-int" type="number" min="0" step="0.1" data-idx="${i}" value="${esc((s.interval || 0) / 1000)}"></td>
            <td class="ptz-mc-acts">
              <button class="btn btn-icon ptz-mc-up" data-idx="${i}" type="button" title="${esc(tr("plugin.ptz.mc.up", "Monter"))}"${i === 0 ? " disabled" : ""}>↑</button>
              <button class="btn btn-icon ptz-mc-down" data-idx="${i}" type="button" title="${esc(tr("plugin.ptz.mc.down", "Descendre"))}"${i === macroSteps.length - 1 ? " disabled" : ""}>↓</button>
              <button class="btn btn-icon btn-red ptz-mc-del" data-idx="${i}" type="button" title="${esc(tr("plugin.ptz.delete", "Supprimer"))}">✕</button>
            </td></tr>`;
    }

    function renderMacros() {
        const box = $("#ptz-pn-content");
        if (!box) return;
        let opts = "";
        for (let i = 1; i <= macroInfo.macro_count; i++) {
            opts += `<option value="${i}"${i === macroSel ? " selected" : ""}>${esc(tr("plugin.ptz.mc.macro", "Macro"))} ${i}</option>`;
        }
        box.innerHTML = `
          <div class="ptz-pn-assign-h">
            <label class="ptz-selall"><span>${esc(tr("plugin.ptz.mc.macro", "Macro"))}</span>
              <select id="ptz-mc-sel">${opts}</select></label>
            <span class="ptz-meta">${macroSteps.length} ${esc(tr("plugin.ptz.mc.steps", "étape(s)"))}</span>
            <span class="ptz-spacer"></span>
            <button class="btn" id="ptz-mc-play" type="button" title="${esc(tr("plugin.ptz.mc.playTip", "Exécute la macro sur les caméras réelles"))}">▶ ${esc(tr("plugin.ptz.mc.play", "Jouer"))}</button>
            <button class="btn" id="ptz-mc-stop" type="button">⏹ ${esc(tr("plugin.ptz.mc.stop", "Arrêter"))}</button>
            <button class="btn btn-green" id="ptz-mc-save" type="button">${esc(tr("plugin.ptz.save", "Enregistrer"))}</button>
          </div>
          <div class="ptz-pn-table-wrap">
          <table class="ptz-tbl ptz-mc-table">
            <thead><tr>
              <th>#</th><th>${esc(tr("plugin.ptz.pn.type", "Type"))}</th>
              <th>${esc(tr("plugin.ptz.camera", "Caméra"))}</th>
              <th>${esc(tr("plugin.ptz.mc.param", "Paramètre"))}</th>
              <th>${esc(tr("plugin.ptz.mc.intervalS", "Intervalle (s)"))}</th><th></th>
            </tr></thead>
            <tbody>${macroSteps.map(macroStepRow).join("")
              || `<tr><td colspan="6" class="ptz-meta">${esc(tr("plugin.ptz.mc.empty", "Macro vide — ajoutez des étapes."))}</td></tr>`}</tbody>
          </table></div>
          <button class="btn" id="ptz-mc-add" type="button">+ ${esc(tr("plugin.ptz.mc.add", "Ajouter une étape"))}</button>`;

        $("#ptz-mc-sel").addEventListener("change", (e) => loadMacro(e.target.value));
        $("#ptz-mc-add").addEventListener("click", () => {
            macroSteps.push({ type: "recall_preset", cam: "", param: "1", interval: 0, camera: null });
            renderMacros();
        });
        $("#ptz-mc-save").addEventListener("click", saveMacro);
        $("#ptz-mc-play").addEventListener("click", () => macroControl(true));
        $("#ptz-mc-stop").addEventListener("click", () => macroControl(false));
        box.querySelectorAll(".ptz-mc-type").forEach((el) => el.addEventListener("change", () => {
            const s = macroSteps[+el.dataset.idx];
            s.type = el.value;
            if (macroTypeDef(s.type).param === "none") s.param = "";
            renderMacros();      // le type change les contrôles (caméra/paramètre)
        }));
        box.querySelectorAll(".ptz-mc-cam").forEach((el) => el.addEventListener("change",
            () => { macroSteps[+el.dataset.idx].cam = el.value.trim(); }));
        box.querySelectorAll(".ptz-mc-param").forEach((el) => el.addEventListener("change",
            () => { macroSteps[+el.dataset.idx].param = el.value.trim(); }));
        box.querySelectorAll(".ptz-mc-int").forEach((el) => el.addEventListener("change",
            () => { macroSteps[+el.dataset.idx].interval = Math.round((parseFloat(el.value) || 0) * 1000); }));
        box.querySelectorAll(".ptz-mc-up").forEach((el) => el.addEventListener("click", () => moveStep(+el.dataset.idx, -1)));
        box.querySelectorAll(".ptz-mc-down").forEach((el) => el.addEventListener("click", () => moveStep(+el.dataset.idx, 1)));
        box.querySelectorAll(".ptz-mc-del").forEach((el) => el.addEventListener("click", () => {
            macroSteps.splice(+el.dataset.idx, 1); renderMacros();
        }));
    }

    function moveStep(i, dir) {
        const j = i + dir;
        if (j < 0 || j >= macroSteps.length) return;
        const t = macroSteps[i]; macroSteps[i] = macroSteps[j]; macroSteps[j] = t;
        renderMacros();
    }

    async function saveMacro() {
        if (macroSteps.length > macroInfo.max_steps) {
            toast(tr("plugin.ptz.mc.tooMany", "Trop d'étapes") + " (" + macroInfo.max_steps + ")", "error");
            return;
        }
        const msg = tr("plugin.ptz.mc.saveConfirm",
            "Enregistrer cette macro dans le pupitre ?") + ` — Macro ${macroSel}`;
        if (!window.confirm(msg)) return;
        const steps = macroSteps.map((s) => ({ type: s.type, cam: s.cam, param: s.param, interval: s.interval }));
        try {
            const d = await ctx.api("panels/" + selPanel + "/macros/" + macroSel, { body: { steps } });
            macroSteps = (d.steps || []).map((s) => ({
                type: s.type, cam: s.cam || "", param: s.param || "",
                interval: s.interval || 0, camera: s.camera || null,
            }));
            toast(tr("plugin.ptz.mc.saved", "Macro enregistrée"), "success");
            renderMacros();
        } catch (e) { toast(e.message, "error"); }
    }

    async function macroControl(play) {
        if (play && !window.confirm(tr("plugin.ptz.mc.playConfirm",
            "Jouer la macro va piloter les caméras réelles. Continuer ?"))) return;
        try {
            await ctx.api("panels/" + selPanel + "/macros/" + macroSel + "/control", { body: { play } });
            toast(play ? tr("plugin.ptz.mc.playing", "Macro lancée")
                       : tr("plugin.ptz.mc.stopped", "Macro arrêtée"), "info");
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Grille (numéros + placement dans les pupitres) ───────
    // Un écran unique : lignes = caméras, colonne N° éditable, puis un bloc de N
    // colonnes par pupitre (positions 1..N). Une position ne tient qu'UNE caméra ; une caméra
    // occupe au plus une position par pupitre. Rien n'est écrit avant « Enregistrer ». Aucun
    // backend dédié : on lit /cameras + /panels/<id>/assignments et on réécrit via ces routes.
    // Dimensions réglables (chargées depuis /config au moment du loadGrid). Défauts prudents
    // si le réglage n'est pas encore lu.
    let gridLimits = { cameras: 100, panels: 10, buttons: 10 };
    let gridCams = [];
    let gridPanels = [];               // [{id, name, slots:{pos:camId|null}, error?}]
    // La numérotation ne se fait PLUS ici (formulaire caméra + matrice Ember+) : la grille
    // ne sert qu'à l'AFFECTATION aux boutons de pupitre. Le numéro reste affiché en lecture,
    // et sert de clé de tri.
    let gridOrig = "";                 // instantané des affectations, pour détecter les changements

    function gridSnapshot() {
        return JSON.stringify(gridPanels.map((p) => ({ id: p.id, s: p.slots })));
    }

    async function loadGrid() {
        const body = $("#ptz-gr-body");
        body.innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.loading", "Chargement…"))}</div>`;
        let cs, ps, cfg;
        try {
            cfg = await ctx.api("config");
            cs = await ctx.api("cameras");
            ps = await ctx.api("panels");
        } catch (e) { body.innerHTML = `<div class="ptz-empty fail">${esc(e.message)}</div>`; return; }
        if (cfg && cfg.grid) gridLimits = cfg.grid;
        gridCams = ((cs && cs.cameras) || []).slice();
        gridPanels = [];
        for (const p of ((ps && ps.panels) || []).slice(0, gridLimits.panels)) {
            try {
                const a = await ctx.api("panels/" + p.id + "/assignments");
                const slots = {};
                (a.assignments || []).forEach((r) => {
                    if (r.slot <= gridLimits.buttons) slots[r.slot] = (r.camera && r.camera.id) || null;
                });
                gridPanels.push({ id: p.id, name: p.name || p.host, slots });
            } catch (e) {
                gridPanels.push({ id: p.id, name: p.name || p.host, error: e.message, slots: {} });
            }
        }
        gridOrig = gridSnapshot();
        renderGrid();
    }

    // Tri par numéro de caméra (croissant, sans numéro en dernier), puis nom.
    function gridSortedCams() {
        return gridCams.slice().sort((a, b) => {
            const va = a.cam_number == null ? Infinity : a.cam_number;
            const vb = b.cam_number == null ? Infinity : b.cam_number;
            if (va !== vb) return va - vb;
            return (a.name || "").localeCompare(b.name || "");
        });
    }

    function renderGrid() {
        const body = $("#ptz-gr-body");
        if (!gridCams.length) {
            body.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.none", "Aucune caméra."))}</div>`;
            return;
        }
        const cams = gridSortedCams().filter(isNumbered).slice(0, gridLimits.cameras);
        const posHdr = [];
        for (let i = 1; i <= gridLimits.buttons; i++) posHdr.push(`<th class="ptz-gr-pos">${i}</th>`);
        // En-tête à deux étages : noms de pupitres (colspan), puis positions.
        const panelNameHdr = gridPanels.map((p) =>
            `<th class="ptz-gr-panelname" colspan="${gridLimits.buttons}">${esc(p.name)}${p.error ? " ⚠" : ""}</th>`).join("");
        const posRow = gridPanels.map(() => posHdr.join("")).join("");

        const cellsFor = (c) => gridPanels.map((p, pi) => {
            if (p.error) return `<td class="ptz-out-na"></td>`.repeat(gridLimits.buttons);
            let out = "";
            for (let pos = 1; pos <= gridLimits.buttons; pos++) {
                const on = p.slots[pos] === c.id;
                out += `<td class="ptz-gr-cell${on ? " on" : ""}" data-p="${pi}" data-pos="${pos}" data-cam="${esc(c.id)}">${on ? "●" : ""}</td>`;
            }
            return out;
        }).join("");

        body.innerHTML = `<div class="ptz-ov-scroll"><table class="ptz-tbl ptz-gr-tbl">
            <thead>
              <tr><th rowspan="2" class="ptz-gr-num">${esc(tr("plugin.ptz.pn.slot", "N°"))}</th>
                  <th rowspan="2">${esc(tr("plugin.ptz.camera", "Caméra"))}</th>${panelNameHdr}</tr>
              <tr>${posRow}</tr>
            </thead>
            <tbody>${cams.map((c) => `<tr>
                <td class="ptz-pn-slot">${c.cam_number != null ? esc(c.cam_number) : "—"}</td>
                <td class="ptz-gr-camname">${esc(c.name)}</td>
                ${cellsFor(c)}
              </tr>`).join("")}</tbody>
          </table></div>`;

        body.querySelectorAll(".ptz-gr-cell").forEach((el) => el.addEventListener("click",
            () => gridCellClick(+el.dataset.p, +el.dataset.pos, el.dataset.cam)));
        $("#ptz-gr-meta").textContent = `${cams.length} ${tr("plugin.ptz.camera", "caméras")} · `
            + `${gridPanels.length} ${tr("plugin.ptz.panels", "pupitres")}`;
        refreshGridPending();
    }

    function gridCellClick(pi, pos, camId) {
        const p = gridPanels[pi];
        if (!p || p.error) return;
        if (p.slots[pos] === camId) {
            p.slots[pos] = null;                 // reclic = désaffecter
        } else {
            // une position ne tient qu'une caméra (la cellule remplace) ; et une caméra
            // n'occupe qu'une position par pupitre → on retire ses autres positions ici.
            for (let q = 1; q <= gridLimits.buttons; q++) if (p.slots[q] === camId) p.slots[q] = null;
            p.slots[pos] = camId;
        }
        renderGrid();
    }

    function refreshGridPending() {
        const n = gridChangeCount();
        $("#ptz-gr-pending").textContent = n ? `${n} ${tr("plugin.ptz.gr.changes", "changement(s)")}` : "";
        $("#ptz-gr-save").disabled = !n;
    }

    function gridChangeCount() {
        let n = 0;
        const orig = JSON.parse(gridOrig || "[]");
        orig.forEach((op, i) => {
            const cur = gridPanels[i] ? gridPanels[i].slots : {};
            for (let pos = 1; pos <= gridLimits.buttons; pos++)
                if ((op.s || {})[pos] !== undefined && (op.s[pos] || null) !== (cur[pos] || null)) n++;
        });
        return n;
    }

    async function saveGrid() {
        const orig = JSON.parse(gridOrig || "[]");
        const panelWrites = gridPanels.map((p, i) => {
            if (p.error) return null;
            const op = orig[i] || { s: {} };
            const rows = [];
            for (let pos = 1; pos <= gridLimits.buttons; pos++) {
                const now = p.slots[pos] || null, was = (op.s || {})[pos] || null;
                if (now === was) continue;
                rows.push(now ? { slot: pos, camera_id: now } : { slot: pos, control_type: "3" });
            }
            return rows.length ? { id: p.id, rows } : null;
        }).filter(Boolean);
        if (!panelWrites.length) return;

        if (!window.confirm(tr("plugin.ptz.gr.saveConfirm2",
            "Enregistrer les affectations de pupitre ? (écriture d'exploitation)"))) return;
        $("#ptz-gr-save").disabled = true;
        try {
            for (const w of panelWrites) {
                await ctx.api("panels/" + w.id + "/assignments", { body: { rows: w.rows } });
            }
            toast(tr("plugin.ptz.gr.saved", "Grille enregistrée"), "success");
            invalidateViews();
            await loadGrid();
        } catch (e) {
            toast(e.message, "error");
            $("#ptz-gr-save").disabled = false;
        }
    }

    // ── Sélection presta (parc fixe → numéroter les caméras utilisées, profils, export) ──
    let psNums = {};                   // camId -> numéro (chaîne, édité)
    let psOrig = {};                   // pour détecter les changements
    let psProfiles = [];
    let psActive = null;

    async function loadPresta() {
        const body = $("#ptz-ps-body");
        body.innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.loading", "Chargement…"))}</div>`;
        let cs, pr;
        try { cs = await ctx.api("cameras"); pr = await ctx.api("prestas"); }
        catch (e) { body.innerHTML = `<div class="ptz-empty fail">${esc(e.message)}</div>`; return; }
        cams = (cs && cs.cameras) || cams;
        psProfiles = (pr && pr.names) || [];
        psActive = (pr && pr.active) || null;
        psNums = {}; cams.forEach((c) => { psNums[c.id] = c.cam_number == null ? "" : String(c.cam_number); });
        psOrig = Object.assign({}, psNums);
        renderPresta();
    }

    function psSorted() {
        return cams.slice().sort((a, b) => {
            const va = a.cam_number == null ? Infinity : a.cam_number;
            const vb = b.cam_number == null ? Infinity : b.cam_number;
            if (va !== vb) return va - vb;
            return (a.name || "").localeCompare(b.name || "");
        });
    }

    function renderPresta() {
        const box = $("#ptz-ps-body");
        const sel = $("#ptz-ps-profile");
        sel.innerHTML = `<option value="">${esc(tr("plugin.ptz.ps.none", "—"))}</option>`
            + psProfiles.map((n) => `<option value="${esc(n)}"${n === psActive ? " selected" : ""}>${esc(n)}</option>`).join("");
        const used = cams.filter(isNumbered).length;
        $("#ptz-ps-meta").textContent = `${used}/${cams.length} ${tr("plugin.ptz.ps.used", "utilisées")}`
            + (psActive ? ` · ${tr("plugin.ptz.ps.current", "profil")} « ${psActive} »` : "");
        if (!cams.length) {
            box.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.none", "Aucune caméra au parc."))}</div>`;
            refreshPsApply(); return;
        }
        box.innerHTML = `<div class="ptz-ov-scroll"><table class="ptz-tbl ptz-ps-table">
            <thead><tr>
              <th>${esc(tr("plugin.ptz.pn.slot", "N°"))}</th>
              <th>${esc(tr("plugin.ptz.camera", "Caméra"))}</th>
              <th>${esc(tr("plugin.ptz.f.host", "Adresse IP"))}</th>
              <th>${esc(tr("plugin.ptz.f.model", "Modèle"))}</th>
            </tr></thead>
            <tbody>${psSorted().map((c) => `<tr>
                <td><input class="ptz-ps-num" data-cam="${esc(c.id)}" type="number" min="1"
                     value="${esc(psNums[c.id] || "")}" style="width:64px"></td>
                <td>${esc(c.name)}</td>
                <td class="ptz-ip">${esc(c.host)}</td>
                <td class="ptz-meta">${esc((c.identity && c.identity.model) || c.model || "")}</td>
              </tr>`).join("")}</tbody>
          </table></div>`;
        box.querySelectorAll(".ptz-ps-num").forEach((el) => el.addEventListener("input",
            () => { psNums[el.dataset.cam] = el.value.trim(); refreshPsApply(); }));
        refreshPsApply();
    }

    function psChanged() {
        return Object.keys(psNums).filter((id) => (psNums[id] || "") !== (psOrig[id] || ""));
    }
    function refreshPsApply() {
        const b = $("#ptz-ps-apply");
        if (b) b.disabled = psChanged().length === 0;
    }

    async function psApplyNumbers() {
        // doublons côté client (message clair)
        const seen = {};
        for (const id in psNums) {
            const v = psNums[id]; if (!v) continue;
            if (seen[v]) throw new Error(tr("plugin.ptz.gr.dupNum", "Numéro en double : ") + v);
            seen[v] = id;
        }
        for (const id of psChanged()) {
            await ctx.api("cameras/" + id, { method: "PUT", body: { cam_number: psNums[id] } });
        }
    }

    async function prestaApply() {
        try {
            await psApplyNumbers();
            toast(tr("plugin.ptz.ps.applied", "Numéros appliqués"), "success");
            invalidateViews(); await loadPresta(); await refresh();
        } catch (e) { toast(e.message, "error"); }
    }

    async function prestaSaveAs() {
        const name = (window.prompt(tr("plugin.ptz.ps.namePrompt", "Nom du profil de presta :")) || "").trim();
        if (!name) return;
        try {
            await psApplyNumbers();                       // fige d'abord les numéros édités
            await ctx.api("prestas", { body: { name } });
            toast(tr("plugin.ptz.ps.saved", "Profil enregistré"), "success");
            invalidateViews(); await loadPresta();
        } catch (e) { toast(e.message, "error"); }
    }

    async function prestaLoad() {
        const name = $("#ptz-ps-profile").value;
        if (!name) return;
        if (!window.confirm(tr("plugin.ptz.ps.loadConfirm",
            "Charger ce profil ? La numérotation actuelle sera remplacée.") + "\n« " + name + " »")) return;
        try {
            await ctx.api("prestas/apply", { body: { name } });
            toast(tr("plugin.ptz.ps.loaded", "Profil chargé"), "success");
            invalidateViews(); await loadPresta(); await refresh();
        } catch (e) { toast(e.message, "error"); }
    }

    async function prestaDelete() {
        const name = $("#ptz-ps-profile").value;
        if (!name) return;
        if (!window.confirm(tr("plugin.ptz.ps.delConfirm", "Supprimer ce profil ?") + "\n« " + name + " »")) return;
        try {
            await ctx.api("prestas/delete", { body: { name } });
            toast(tr("plugin.ptz.ps.deleted", "Profil supprimé"), "success");
            await loadPresta();
        } catch (e) { toast(e.message, "error"); }
    }

    async function prestaExport() {
        try {
            const data = await ctx.api("export");
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob); a.download = "parc-ptz.json";
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 1000);
        } catch (e) { toast(e.message, "error"); }
    }

    async function prestaImport(e) {
        const f = e.target.files && e.target.files[0];
        e.target.value = "";
        if (!f) return;
        try {
            const data = JSON.parse(await f.text());
            const rep = await ctx.api("import", { body: { data } });
            toast(`${rep.added || 0} ${tr("plugin.ptz.ps.added", "ajoutée(s)")}, `
                + `${rep.updated || 0} ${tr("plugin.ptz.ps.updated", "mise(s) à jour")}`, "success");
            invalidateViews(); await loadPresta(); await refresh();
        } catch (err) { toast(err.message || tr("plugin.ptz.ps.badImport", "fichier d'import invalide"), "error"); }
    }

    // ── Shotbox (grille de boutons : rappel mémoire d'une caméra, ou macro) ──
    let sbBoxes = {};                  // {name: box}
    let sbName = null;                 // shotbox affichée
    let sbBox = null;                  // copie de travail {rows, cols, buttons:{idx:{...}}}
    let sbMode = "use";                // "use" | "edit"
    let sbSel = null;                  // index de bouton sélectionné (édition)
    let sbPanels = [];
    let sbDirty = false;

    async function loadShotbox() {
        const body = $("#ptz-sb-body");
        body.innerHTML = `<div class="ptz-meta">${esc(tr("plugin.ptz.loading", "Chargement…"))}</div>`;
        let sb, cs, ps;
        try { sb = await ctx.api("shotboxes"); cs = await ctx.api("cameras"); ps = await ctx.api("panels"); }
        catch (e) { body.innerHTML = `<div class="ptz-empty fail">${esc(e.message)}</div>`; return; }
        cams = (cs && cs.cameras) || cams;
        sbPanels = (ps && ps.panels) || [];
        sbBoxes = (sb && sb.boxes) || {};
        const names = Object.keys(sbBoxes);
        if (!sbName || !sbBoxes[sbName]) sbName = names[0] || null;
        sbBox = sbName ? JSON.parse(JSON.stringify(sbBoxes[sbName])) : null;
        sbDirty = false; sbSel = null;
        renderShotbox();
    }

    function sbSetMode(m) { sbMode = m; sbSel = null; renderShotbox(); }

    // Plein écran « scène » : on force le mode Utilisation, on masque tout le reste (une classe
    // superpose la vue en plein cadre) et on demande le vrai plein écran navigateur si dispo.
    function sbSetFull(on) {
        const view = $("#ptz-view-shotbox");
        if (!view) return;
        if (on) {
            if (sbMode !== "use") sbSetMode("use");
            view.classList.add("ptz-sb-full");
            $("#ptz-sb-exit").hidden = false;
            if (view.requestFullscreen) { try { view.requestFullscreen(); } catch (e) { /* ignore */ } }
            renderShotbox();
        } else {
            view.classList.remove("ptz-sb-full");
            $("#ptz-sb-exit").hidden = true;
            if (document.fullscreenElement && document.exitFullscreen) { try { document.exitFullscreen(); } catch (e) { /* ignore */ } }
        }
    }
    function sbOnFsChange() {
        if (!root || document.fullscreenElement) return;   // sortie (Échap) → on nettoie l'overlay
        const v = $("#ptz-view-shotbox"); if (v) v.classList.remove("ptz-sb-full");
        const x = $("#ptz-sb-exit"); if (x) x.hidden = true;
    }
    function sbSelectBox(name) {
        if (sbDirty && !window.confirm(tr("plugin.ptz.sb.dropChanges", "Abandonner les changements non enregistrés ?"))) {
            $("#ptz-sb-sel").value = sbName; return;
        }
        sbName = name || null;
        sbBox = sbName ? JSON.parse(JSON.stringify(sbBoxes[sbName])) : null;
        sbDirty = false; sbSel = null; renderShotbox();
    }

    const sbBtn = (i) => (sbBox.buttons && sbBox.buttons[String(i)]) || {};
    function sbBtnLabel(b) {
        if (b.label) return b.label;
        if (b.type === "preset") return "N°" + (b.cam || "?") + " ▸ M" + (b.preset || "?");
        if (b.type === "macro") {
            const p = sbPanels.find((x) => x.id === b.panel);
            return "▶ " + tr("plugin.ptz.mc.macro", "Macro") + " " + (b.macro || "?") + (p ? " · " + (p.name || p.host) : "");
        }
        return "";
    }

    function renderShotbox() {
        // sélecteur + modes + save
        const sel = $("#ptz-sb-sel");
        const names = Object.keys(sbBoxes);
        sel.innerHTML = names.map((n) => `<option value="${esc(n)}"${n === sbName ? " selected" : ""}>${esc(n)}</option>`).join("")
            || `<option value="">${esc(tr("plugin.ptz.sb.noBox", "(aucune)"))}</option>`;
        root.querySelectorAll("#ptz-sb-modes .ptz-pn-modebtn").forEach((b) =>
            b.classList.toggle("on", b.dataset.mode === sbMode));
        $("#ptz-sb-save").disabled = !sbDirty;
        $("#ptz-sb-del").disabled = !sbName;

        const box = $("#ptz-sb-body");
        if (!sbBox) {
            box.innerHTML = `<div class="ptz-empty">${esc(tr("plugin.ptz.sb.none", "Aucune shotbox — cliquez « Nouvelle… »."))}</div>`;
            return;
        }
        const rows = sbBox.rows || 4, cols = sbBox.cols || 4;
        let dims = "";
        if (sbMode === "edit") {
            dims = `<div class="ptz-sb-dims">
                <label>${esc(tr("plugin.ptz.sb.rows", "Lignes"))} <input id="ptz-sb-rows" type="number" min="1" max="20" value="${rows}" style="width:56px"></label>
                <label>${esc(tr("plugin.ptz.sb.cols", "Colonnes"))} <input id="ptz-sb-cols" type="number" min="1" max="20" value="${cols}" style="width:56px"></label>
              </div>`;
        }
        let cells = "";
        for (let i = 0; i < rows * cols; i++) {
            const b = sbBtn(i);
            const filled = b.type ? " filled" : "";
            const seln = (sbMode === "edit" && sbSel === i) ? " on" : "";
            cells += `<button class="ptz-sb-cell${filled}${seln}" data-i="${i}" type="button">${esc(sbBtnLabel(b))}</button>`;
        }
        const grid = `<div class="ptz-sb-grid" style="grid-template-columns:repeat(${cols},1fr)">${cells}</div>`;
        box.innerHTML = dims + grid + (sbMode === "edit" ? `<div id="ptz-sb-editor"></div>` : "");

        if (sbMode === "edit") {
            $("#ptz-sb-rows").addEventListener("change", (e) => { sbBox.rows = Math.max(1, Math.min(20, parseInt(e.target.value, 10) || 1)); sbDirty = true; renderShotbox(); });
            $("#ptz-sb-cols").addEventListener("change", (e) => { sbBox.cols = Math.max(1, Math.min(20, parseInt(e.target.value, 10) || 1)); sbDirty = true; renderShotbox(); });
        }
        box.querySelectorAll(".ptz-sb-cell").forEach((el) => el.addEventListener("click",
            () => (sbMode === "edit" ? sbEditButton(+el.dataset.i) : sbFire(+el.dataset.i, el))));
        if (sbMode === "edit" && sbSel != null) renderSbEditor();
    }

    function sbEditButton(i) { sbSel = i; renderShotbox(); }

    function renderSbEditor() {
        const ed = $("#ptz-sb-editor");
        if (!ed) return;
        const b = sbBtn(sbSel);
        const numbered = cams.filter(isNumbered).slice().sort((a, c) => (a.cam_number || 0) - (c.cam_number || 0));
        const camOpts = `<option value="">—</option>` + numbered.map((c) =>
            `<option value="${c.cam_number}"${String(b.cam || "") === String(c.cam_number) ? " selected" : ""}>N°${c.cam_number} · ${esc(c.name)}</option>`).join("");
        const panelOpts = `<option value="">—</option>` + sbPanels.map((p) =>
            `<option value="${esc(p.id)}"${b.panel === p.id ? " selected" : ""}>${esc(p.name || p.host)}</option>`).join("");
        ed.innerHTML = `<div class="ptz-sb-editor">
            <strong>${esc(tr("plugin.ptz.sb.button", "Bouton"))} ${sbSel + 1}</strong>
            <label>${esc(tr("plugin.ptz.sb.label", "Libellé"))} <input id="ptz-sb-label" type="text" value="${esc(b.label || "")}" placeholder="${esc(tr("plugin.ptz.sb.auto", "(auto)"))}"></label>
            <label>${esc(tr("plugin.ptz.pn.type", "Type"))}
              <select id="ptz-sb-type">
                <option value=""${!b.type ? " selected" : ""}>${esc(tr("plugin.ptz.sb.tNone", "— vide —"))}</option>
                <option value="preset"${b.type === "preset" ? " selected" : ""}>${esc(tr("plugin.ptz.sb.tPreset", "Rappel mémoire"))}</option>
                <option value="macro"${b.type === "macro" ? " selected" : ""}>${esc(tr("plugin.ptz.sb.tMacro", "Macro"))}</option>
              </select></label>
            ${b.type === "preset" ? `
              <label>${esc(tr("plugin.ptz.camera", "Caméra"))} <select id="ptz-sb-cam">${camOpts}</select></label>
              <label>${esc(tr("plugin.ptz.sb.preset", "Mémoire"))} <input id="ptz-sb-preset" type="number" min="1" max="100" value="${esc(b.preset || "")}" style="width:64px"></label>` : ""}
            ${b.type === "macro" ? `
              <label>${esc(tr("plugin.ptz.panels", "Pupitre"))} <select id="ptz-sb-panel">${panelOpts}</select></label>
              <label>${esc(tr("plugin.ptz.mc.macro", "Macro"))} <input id="ptz-sb-macro" type="number" min="1" max="100" value="${esc(b.macro || "")}" style="width:64px"></label>` : ""}
            <button class="btn btn-red" id="ptz-sb-clearbtn" type="button">${esc(tr("plugin.ptz.sb.clearBtn", "Vider ce bouton"))}</button>
          </div>`;
        const setB = (patch) => { sbBox.buttons = sbBox.buttons || {}; sbBox.buttons[String(sbSel)] = Object.assign({}, b, patch); sbDirty = true; };
        $("#ptz-sb-label").addEventListener("change", (e) => { setB({ label: e.target.value.trim() }); renderShotbox(); });
        $("#ptz-sb-type").addEventListener("change", (e) => { setB({ type: e.target.value || null }); renderShotbox(); });
        const cam = $("#ptz-sb-cam"); if (cam) cam.addEventListener("change", (e) => { setB({ cam: e.target.value }); renderShotbox(); });
        const pre = $("#ptz-sb-preset"); if (pre) pre.addEventListener("change", (e) => { setB({ preset: e.target.value }); renderShotbox(); });
        const pan = $("#ptz-sb-panel"); if (pan) pan.addEventListener("change", (e) => { setB({ panel: e.target.value }); renderShotbox(); });
        const mac = $("#ptz-sb-macro"); if (mac) mac.addEventListener("change", (e) => { setB({ macro: e.target.value }); renderShotbox(); });
        $("#ptz-sb-clearbtn").addEventListener("click", () => { if (sbBox.buttons) delete sbBox.buttons[String(sbSel)]; sbDirty = true; renderShotbox(); });
    }

    async function sbFire(i, el) {
        const b = sbBtn(i);
        if (!b.type) return;
        el.classList.add("fired"); setTimeout(() => el.classList.remove("fired"), 400);
        try {
            if (b.type === "preset") {
                const c = cams.find((x) => String(x.cam_number) === String(b.cam));
                if (!c) { toast(tr("plugin.ptz.sb.noCam", "Caméra introuvable (numéro non attribué)"), "error"); return; }
                await ctx.api("cameras/" + c.id + "/presets/recall", { body: { index: parseInt(b.preset, 10) } });
                toast(`${tr("plugin.ptz.sb.recalled", "Mémoire rappelée")} · ${c.name}`, "info");
            } else if (b.type === "macro") {
                await ctx.api("panels/" + b.panel + "/macros/" + parseInt(b.macro, 10) + "/control", { body: { play: true } });
                toast(tr("plugin.ptz.sb.macroFired", "Macro lancée"), "info");
            }
        } catch (e) { toast(e.message, "error"); }
    }

    async function sbNew() {
        const name = (window.prompt(tr("plugin.ptz.sb.namePrompt", "Nom de la shotbox :")) || "").trim();
        if (!name) return;
        sbBoxes[name] = sbBoxes[name] || { rows: 4, cols: 4, buttons: {} };
        sbName = name; sbBox = JSON.parse(JSON.stringify(sbBoxes[name]));
        sbMode = "edit"; sbSel = null; sbDirty = true;
        renderShotbox();
    }

    async function sbSave() {
        if (!sbName || !sbBox) return;
        try {
            const r = await ctx.api("shotboxes", { body: { name: sbName, box: sbBox } });
            sbBoxes = (r && r.boxes) || sbBoxes;
            sbDirty = false;
            toast(tr("plugin.ptz.sb.saved", "Shotbox enregistrée"), "success");
            renderShotbox();
        } catch (e) { toast(e.message, "error"); }
    }

    async function sbDelete() {
        if (!sbName) return;
        if (!window.confirm(tr("plugin.ptz.sb.delConfirm", "Supprimer cette shotbox ?") + "\n« " + sbName + " »")) return;
        try {
            await ctx.api("shotboxes/delete", { body: { name: sbName } });
            toast(tr("plugin.ptz.sb.deleted", "Shotbox supprimée"), "success");
            delete sbBoxes[sbName]; sbName = null;
            await loadShotbox();
        } catch (e) { toast(e.message, "error"); }
    }

    return { mount, unmount };
})();
