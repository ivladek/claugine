/* Lists: rows per page (default from Settings, remembered per list), quick filter, a selected row shows its details
   under the list, [data-open] opens a dialog, the add button sits in the header of the actions column.
   Markup: div.olist[data-key] > table.olist-table > tr.orow[data-id]; div.olist-detail > div.odetail[data-for=id]
   (div.odetail[data-src=url] is loaded when first shown). */
(function () {
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function defaultSize() { return (document.body && document.body.dataset.listRows) || "10"; }

  // Page layout: title with its add buttons, short description, filters, list, actions of the selected item, details.
  // The add buttons of a list (.olist-add) go next to its title: the h2 of its bar or of its card when it has one,
  // else the page's h1. A list inside details keeps them in its own bar.
  function headActions(h) {
    var span = h.querySelector(":scope > .head-actions");
    if (!span) { span = document.createElement("span"); span.className = "head-actions"; h.appendChild(span); }
    return span;
  }
  function titleOf(box) {
    var bar = box.querySelector(":scope > .olist-bar");
    var h = bar && bar.querySelector("h2,h3");
    if (h) return h;
    if (box.closest(".odetail, dialog")) return null;
    var card = box.parentElement && box.parentElement.closest(".card");
    if (card && card !== box) {
      h = card.querySelector(":scope > h2, :scope > .olist-bar h2");
      if (h) return h;
    }
    var main = box.closest("main") || document;
    return main.querySelector("h1");
  }
  function placeAdd(box, table) {
    var bar = box.querySelector(":scope > .olist-bar");
    if (!bar) return;
    var tools = bar.querySelector(".olist-tools");
    // add buttons and the list's other actions (forms, links styled as buttons); not the filter or view toggles
    var acts = Array.prototype.slice.call(bar.querySelectorAll(".olist-add, [data-add]"));
    if (tools) Array.prototype.forEach.call(tools.children, function (el) {
      if (acts.indexOf(el) < 0 && !el.matches("input.olist-search, .olist-toggle") && (el.matches(".btn, form") || el.querySelector(".btn"))) acts.push(el);
    });
    var h = acts.length ? titleOf(box) : null;
    if (!h) return;
    var span = headActions(h), first = span.firstChild;
    acts.forEach(function (a) {  // add buttons first, then the list's other actions after what the title has
      if (a.matches(".olist-add, [data-add]")) span.insertBefore(a, first); else span.appendChild(a);
    });
  }
  // The quick filter is offered only when the list has more rows than a page; it sits in the filters line above
  // the list (no line of its own over the last column).
  function placeFilter(box, rows, size) {
    var bar = box.querySelector(":scope > .olist-bar");
    if (!bar) return null;
    var search = bar.querySelector("input.olist-search");
    if (search && rows.length <= size) { search.remove(); search = null; }
    else if (search) {
      var filters = box.querySelector(":scope > .filters") || (box.previousElementSibling && box.previousElementSibling.classList.contains("filters") ? box.previousElementSibling : null);
      if (!filters) {
        filters = document.createElement("div"); filters.className = "filters";
        if (box.closest(".odetail, dialog") || bar.querySelector("h2,h3")) bar.after(filters); else box.before(filters);
      }
      filters.insertBefore(search, filters.firstChild);
    }
    var toggles = bar.querySelectorAll(".olist-toggle");
    if (toggles.length) {
      var f2 = (search && search.parentElement) || document.createElement("div");
      if (!f2.parentElement) { f2.className = "filters"; if (box.closest(".odetail, dialog") || bar.querySelector("h2,h3")) bar.after(f2); else box.before(f2); }
      toggles.forEach(function (t) { f2.appendChild(t); });
    }
    var tools = bar.querySelector(".olist-tools");
    if (tools && !tools.children.length) tools.remove();
    if (!bar.querySelector("h2,h3") && !bar.querySelector(".olist-tools")) bar.remove();
    return search;
  }

  // loading a detail from data-src; forms in it come back to this list after their action
  function load(d) {
    if (!d.dataset.src || d.dataset.loaded) return;
    d.dataset.loaded = "1";
    d.innerHTML = '<p class="muted">…</p>';
    var src = d.dataset.src, here = new URL(location.href);
    if (here.searchParams.get("sel") === d.dataset.for) {  // a dialog to reopen after an action (igw, provision): once
      var su = new URL(src, location.href), moved = false;
      ["igw", "provision"].forEach(function (k) { var v = here.searchParams.get(k); if (v) { su.searchParams.set(k, v); here.searchParams.delete(k); moved = true; } });
      if (moved) { src = su.pathname + su.search; history.replaceState(null, "", here.pathname + here.search + here.hash); }
    }
    fetch(src, { credentials: "same-origin", headers: { "X-Partial": "1" } }).then(function (r) { return r.text(); }).then(function (html) {
      d.innerHTML = html;
      var u = new URL(location.href); u.searchParams.set("sel", d.dataset.for); u.hash = "";
      var back = u.pathname + u.search;
      d.querySelectorAll("form").forEach(function (f) {
        if ((f.method || "").toLowerCase() !== "post" || f.querySelector("input[name=back]")) return;
        var i = document.createElement("input"); i.type = "hidden"; i.name = "back"; i.value = back; f.appendChild(i);
      });
      d.querySelectorAll("script").forEach(function (old) {
        var s = document.createElement("script"); s.textContent = old.textContent; old.replaceWith(s);
      });
      d.querySelectorAll(".olist").forEach(init);
      splits();
    }).catch(function () { d.innerHTML = ""; delete d.dataset.loaded; });
  }

  // the actions of a row go to the top of its details (kept in view while the details scroll); the row stays clean
  function moveActions(box, table, rows) {
    if (box.dataset.actions === "row") return;
    var detail = box.querySelector(":scope > .olist-detail");
    if (!detail) return;
    var moved = 0;
    rows.forEach(function (r) {
      var cell = r.querySelector(":scope > td.row-actions");
      var d = Array.prototype.filter.call(detail.children, function (x) { return x.classList.contains("odetail") && x.dataset.for === r.dataset.id; })[0];
      if (!d || d.dataset.src) return;
      var extra = d.querySelectorAll(".to-bar");  // actions written in the details join the bar
      var has = cell && (cell.textContent.trim() || cell.querySelector("button,a,form"));
      if (!has && !extra.length) return;
      var bar = document.createElement("div"); bar.className = "detail-actions";
      var src = cell ? (cell.querySelector(".stack-right") || cell) : null;
      while (src && src.firstChild) bar.appendChild(src.firstChild);
      extra.forEach(function (p) { while (p.firstChild) bar.appendChild(p.firstChild); p.remove(); });
      bar.querySelectorAll("[style]").forEach(function (e) { e.style.width = ""; });
      var h = d.querySelector(":scope > h3, :scope > h2");
      if (h) h.after(bar); else d.insertBefore(bar, d.firstChild);
      if (cell) cell.innerHTML = "";
      moved++;
    });
    if (moved) box.classList.add("actions-in-detail");
  }

  function init(box) {
    if (box.dataset.olistReady) return;
    box.dataset.olistReady = "1";
    var key = "olist:" + (box.dataset.key || location.pathname);
    var table = box.querySelector("table.olist-table");
    if (!table) return;
    placeAdd(box, table);
    var rows = Array.prototype.slice.call(table.querySelectorAll("tr.orow"));
    moveActions(box, table, rows);
    var size = parseInt(store(key + ":size") || box.dataset.size || defaultSize(), 10);
    var search = placeFilter(box, rows, Math.min(size, parseInt(defaultSize(), 10)));
    var page = 0, query = "";
    var foot = box.querySelector(".olist-foot");
    var detail = box.querySelector(":scope > .olist-detail");
    var sizeSel = box.querySelector("select.olist-size");
    if (sizeSel) { sizeSel.value = String(size); sizeSel.addEventListener("change", function () { size = parseInt(sizeSel.value, 10); store(key + ":size", size); page = 0; render(); }); }
    if (search) search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); page = 0; render(); });

    function visible() { return rows.filter(function (r) { return !query || r.textContent.toLowerCase().indexOf(query) >= 0; }); }
    function render() {
      var vis = visible(), pages = Math.max(1, Math.ceil(vis.length / size));
      if (page >= pages) page = pages - 1;
      rows.forEach(function (r) { r.hidden = true; });
      vis.slice(page * size, page * size + size).forEach(function (r) { r.hidden = false; });
      var info = foot && foot.querySelector(".olist-info");
      if (info) info.textContent = vis.length ? (page * size + 1) + "–" + Math.min(vis.length, page * size + size) + " / " + vis.length : "0";
      var prev = foot && foot.querySelector(".olist-prev"), next = foot && foot.querySelector(".olist-next");
      if (prev) prev.disabled = page === 0;
      if (next) next.disabled = page >= pages - 1;
    }
    if (foot) {
      var p = foot.querySelector(".olist-prev"), n = foot.querySelector(".olist-next");
      if (p) p.addEventListener("click", function () { page--; render(); });
      if (n) n.addEventListener("click", function () { page++; render(); });
    }
    function select(row, scroll) {
      rows.forEach(function (r) { r.classList.toggle("sel", r === row); });
      if (!detail) return;
      var shown = false;
      detail.querySelectorAll(":scope > .odetail").forEach(function (d) { var on = row && d.dataset.for === row.dataset.id; d.hidden = !on; shown = shown || on; if (on) load(d); });
      var empty = detail.querySelector(".empty");
      if (empty) empty.hidden = shown;
      if (row) {
        store(key + ":sel", row.dataset.id);
        var idx = visible().indexOf(row);
        if (idx >= 0 && Math.floor(idx / size) !== page) { page = Math.floor(idx / size); render(); }
        if (scroll && shown) detail.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
    table.addEventListener("click", function (e) {
      if (e.target.closest("a,button,form,input,select,textarea,label,summary,dialog")) return;
      var row = e.target.closest("tr.orow");
      if (row) select(row.classList.contains("sel") ? null : row, true);
    });
    render();
    var want = new URLSearchParams(location.search).get("sel") || store(key + ":sel");
    var start = want && rows.filter(function (r) { return r.dataset.id === want; })[0];
    if (!start && box.dataset.autoselect !== undefined) start = rows[0];
    select(start || null, false);
  }

  // The dialog a [data-open] button opens. Details of several rows stay loaded under a list (the ones not selected are
  // hidden) and each has its own dialogs with the same ids: the dialog is the one next to the button (in its details),
  // else a visible one. Opening a hidden one left the page blocked behind an invisible modal (2026-10-28, Move).
  function dialogFor(o) {
    var id = o.dataset.open, sel = "dialog#" + (window.CSS && CSS.escape ? CSS.escape(id) : id);
    var scope = o.closest(".odetail, .olist-detail > *, section, main") || document;
    var mine = scope.querySelector(sel);
    if (mine) return mine;
    var all = Array.prototype.slice.call(document.querySelectorAll(sel));
    return all.filter(function (d) { return !d.closest("[hidden]"); })[0] || all[0] || null;
  }
  document.addEventListener("click", function (e) {
    var o = e.target.closest("[data-open]");
    if (o) { var d = dialogFor(o); if (d && d.showModal) { e.preventDefault(); d.showModal(); } return; }
    var c = e.target.closest("[data-close]");
    if (c) { var dl = c.closest("dialog"); if (dl) dl.close(); }
  });
  // select[data-fill=<textarea id>]: the chosen option's data-items (space separated) fill the textarea, one per line
  document.addEventListener("change", function (e) {
    var s = e.target.closest && e.target.closest("select[data-fill]");
    if (!s) return;
    var t = document.getElementById(s.dataset.fill), o = s.selectedOptions && s.selectedOptions[0];
    if (t && o && o.dataset.items !== undefined) {
      t.value = o.dataset.items.split(" ").filter(Boolean).join("\n");
      if (t._rowsReload) t._rowsReload();
    }
  });
  function splits() {
    document.querySelectorAll("[data-split]").forEach(function (w) {
      var num = document.getElementById(w.dataset.split), range = w.querySelector("input[type=range]");
      var total = w.dataset.total ? document.getElementById(w.dataset.total) : null;
      var a = w.querySelector(".a"), b = w.querySelector(".b"), la = w.querySelector(".la"), lb = w.querySelector(".lb");
      function show(v) {
        v = Math.max(0, Math.min(100, parseInt(v || 0, 10)));
        a.style.width = v + "%"; b.style.width = (100 - v) + "%";
        var t = total ? parseInt(total.value || 0, 10) : 0;
        var img = t ? Math.round(t * (100 - v) / 100) : null;
        la.textContent = w.dataset.la + " " + v + "%" + (t ? " · " + (t - img) + " GB" : "");
        lb.textContent = w.dataset.lb + " " + (100 - v) + "%" + (t ? " · " + img + " GB" : "");
      }
      if (!num || !range) return;
      var field = num.closest(".field");
      if (field && field.nextElementSibling !== w) field.after(w);
      range.value = num.value || 80;
      range.addEventListener("input", function () { num.value = range.value; show(range.value); });
      num.addEventListener("input", function () { range.value = num.value; show(num.value); });
      if (total) total.addEventListener("input", function () { show(num.value); });
      show(num.value || 80);
    });
  }
  // filter forms (form.filters[data-auto]) apply at once: a choice when made, text after a pause in typing
  function autoFilters() {
    document.querySelectorAll("form.filters[data-auto]").forEach(function (f) {
      if (f.dataset.autoReady) return;
      f.dataset.autoReady = "1";
      var t = null;
      f.addEventListener("change", function (e) { if (e.target.matches("select, input[type=checkbox], input[type=date], input[type=radio]")) f.submit(); });
      f.addEventListener("input", function (e) {
        if (!e.target.matches("input[type=search], input[type=text]")) return;
        clearTimeout(t); t = setTimeout(function () { f.submit(); }, 700);
      });
      var q = f.querySelector("input[type=search], input[type=text]");
      if (q && q.value && document.activeElement === document.body && f.dataset.focus !== "no") { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
    });
  }
  function boot() { document.querySelectorAll(".olist").forEach(init); splits(); autoFilters(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
/* Password policy: ul.pw-policy[data-policy] lists the rules; typing a password marks each rule met or not.
   The list follows its field, or (data-scope=form) every password field of its form. */
(function () {
  function count(s, re) { return (s.match(re) || []).length; }
  function check(ul, v) {
    var p; try { p = JSON.parse(ul.dataset.policy); } catch (e) { return; }
    var sp = 0, bad = false;
    for (var i = 0; i < v.length; i++) {
      var c = v[i];
      if (p.specials.indexOf(c) >= 0) sp++;
      if (!/[A-Za-z0-9]/.test(c) && p.specials.indexOf(c) < 0) bad = true;
    }
    var ok = { length: v.length >= p.length, lower: count(v, /[a-z]/g) >= p.lower, upper: count(v, /[A-Z]/g) >= p.upper,
               digits: count(v, /[0-9]/g) >= p.digits, special: sp >= p.special, forbidden: !bad };
    ul.querySelectorAll("li[data-rule]").forEach(function (li) {
      var r = li.dataset.rule;
      li.classList.remove("ok", "bad");
      if (v && r in ok) li.classList.add(ok[r] ? "ok" : "bad");
    });
  }
  document.addEventListener("input", function (e) {
    var i = e.target;
    if (!i || (i.type !== "password" && !(i.dataset && i.dataset.pwEye)) || (i.autocomplete && i.autocomplete.indexOf("new-password") < 0 && !i.classList.contains("pw-check"))) return;
    var f = i.closest(".field");
    var ul = (f && f.querySelector("ul.pw-policy")) || (i.form && i.form.querySelector("ul.pw-policy[data-scope=form]"));
    if (ul) check(ul, i.value);
  });
})();
/* Row lists: textarea[data-rows] (one item per line, fields separated by data-sep, default a space) is shown as a
   table of inputs with + and × buttons; the textarea keeps the lines the server reads.
   data-rows='[{"n": "subnet", "l": "Subnet", "p": "10.0.0.0/16", "o": ["a", "b"], "w": "8em"}, ...]' */
(function () {
  function build(ta) {
    if (ta.dataset.rowsReady) return;
    ta.dataset.rowsReady = "1";
    var cols; try { cols = JSON.parse(ta.dataset.rows); } catch (e) { return; }
    var sep = ta.dataset.sep || " ";
    // options computed from another row list: the ports of the network cards (<name>f<i>, np<i> for Mellanox/NVIDIA)
    function dynamic() {
      var src = ta.dataset.portsFrom && document.getElementById(ta.dataset.portsFrom);
      if (!src) return [];
      var out = [];
      src.value.split("\n").forEach(function (line) {
        var f = line.split("|").map(function (x) { return x.trim(); });
        if (f.length < 5 || !f[1]) return;
        var n = parseInt(f[4], 10) || 0, mlx = /mellanox|nvidia|connectx|mcx/i.test(f[2] + " " + f[3]);
        if (n === 1) out.push(f[1] + (mlx ? "np0" : ""));  // a one-function card has no f0 (systemd)
        else for (var i = 0; i < n; i++) out.push(f[1] + "f" + i + (mlx ? "np" + i : ""));
      });
      return out;
    }
    var box = document.createElement("div"); box.className = "rowlist";
    var table = document.createElement("table"); table.className = "rowlist-table";
    var head = document.createElement("tr");
    cols.forEach(function (c) { var th = document.createElement("th"); th.textContent = c.l; head.appendChild(th); });
    head.appendChild(document.createElement("th"));
    var thead = document.createElement("thead"); thead.appendChild(head); table.appendChild(thead);
    var body = document.createElement("tbody"); table.appendChild(body);
    var add = document.createElement("button"); add.type = "button"; add.className = "btn ghost small"; add.textContent = "+ " + (ta.dataset.addLabel || "Add");
    function sync() {
      var lines = [];
      body.querySelectorAll("tr").forEach(function (tr) {
        var vals = Array.prototype.map.call(tr.querySelectorAll("input,select"), function (i) { return i.value.trim(); });
        if (vals.some(function (v) { return v; })) {
          while (vals.length && !vals[vals.length - 1]) vals.pop();
          lines.push(vals.join(sep === " " ? " " : " " + sep + " "));
        }
      });
      ta.value = lines.join("\n");
    }
    function row(vals) {
      var tr = document.createElement("tr");
      cols.forEach(function (c, i) {
        var td = document.createElement("td"), inp;
        var v0 = (vals[i] || "").trim();
        if (c.o) {
          inp = document.createElement("select");
          var fill = function (sel, opts) {
            var keep = sel.value || v0;
            sel.innerHTML = "";
            [""].concat(opts).concat(keep && opts.indexOf(keep) < 0 ? [keep] : []).forEach(function (o) {
              var op = document.createElement("option"); op.value = o; op.textContent = o || "—"; sel.appendChild(op); });
            sel.value = keep;
          };
          fill(inp, c.dyn ? dynamic() : c.o.filter(Boolean));
          if (c.dyn) inp.addEventListener("focus", function () { fill(this, dynamic()); });
        } else { inp = document.createElement("input"); inp.placeholder = c.p || ""; }
        inp.value = v0;
        if (c.w) inp.style.width = c.w;
        inp.setAttribute("aria-label", c.l);
        inp.addEventListener("input", sync); inp.addEventListener("change", sync);
        td.appendChild(inp); tr.appendChild(td);
      });
      var td = document.createElement("td"), x = document.createElement("button");
      x.type = "button"; x.className = "link-btn"; x.textContent = "×"; x.title = "Remove";
      x.addEventListener("click", function () { tr.remove(); sync(); });
      td.appendChild(x); tr.appendChild(td);
      body.appendChild(tr);
    }
    function load() {
      body.innerHTML = "";
      ta.value.split("\n").forEach(function (line) {
        if (!line.trim()) return;
        var vals = sep === " " ? line.trim().split(/\s+/) : line.split(sep);
        if (sep === " " && vals.length > cols.length) vals = vals.slice(0, cols.length - 1).concat([vals.slice(cols.length - 1).join(" ")]);
        row(vals);
      });
      if (!body.children.length) row([]);
    }
    ta._rowsReload = load;
    load();
    ta.required = false;  // hidden: the server checks it
    add.addEventListener("click", function () { row([]); });
    box.appendChild(table); box.appendChild(add);
    ta.style.display = "none";
    ta.after(box);
  }
  function all(root) { (root || document).querySelectorAll("textarea[data-rows]").forEach(build); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { all(); }); else all();
  document.addEventListener("click", function (e) { var o = e.target.closest("[data-open]"); if (o) setTimeout(function () { all(); }, 0); });
})();
/* Secret fields: a kept value shows as "set" (placeholder); the "Show values" switch (input[data-reveal-url]) asks
   the server for the values (logged), shows them in the fields and hides them again when switched off. */
(function () {
  // a selected (focused) field that keeps a value shows it; leaving the field unchanged hides it again
  function valuesOf(box) {
    var sw = box.querySelector("input[data-reveal-url]");
    if (!sw) return null;
    if (!box._values) {
      var csrf = box.querySelector("input[name=csrfmiddlewaretoken]") || document.querySelector("input[name=csrfmiddlewaretoken]");
      box._values = fetch(sw.dataset.revealUrl, {method: "POST", credentials: "same-origin",
                                                 headers: {"X-CSRFToken": csrf ? csrf.value : "", "X-Requested-With": "fetch"}})
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .catch(function () { box._values = null; return {}; });
    }
    return box._values;
  }
  document.addEventListener("focusin", function (e) {
    var el = e.target;
    if (!el.matches || !el.matches('[data-secret="set"]') || el.value || el.dataset.revealed) return;
    var box = el.closest("form") || el.closest("dialog");
    var p = box && valuesOf(box);
    if (!p) return;
    p.then(function (data) {
      var v = data[el.name];
      if (v === undefined || el.value || document.activeElement !== el) return;
      el.value = v; el.dataset.revealed = v; el.dataset.focusShown = "1";
      if (el.tagName === "INPUT") el.type = "text";
    });
  });
  document.addEventListener("focusout", function (e) {
    var el = e.target;
    if (!el.dataset || !el.dataset.focusShown) return;
    if (el.value === el.dataset.revealed) {
      el.value = "";
      if (el.tagName === "INPUT") el.type = "password";
      delete el.dataset.revealed;
    }
    delete el.dataset.focusShown;
  });
  // values shown but not changed are not sent again: the kept ones stay as they are
  document.addEventListener("submit", function (e) {
    e.target.querySelectorAll("[data-revealed]").forEach(function (el) {
      if (el.value === el.dataset.revealed) el.value = "";
    });
  }, true);
  document.addEventListener("change", function (e) {
    var sw = e.target;
    if (!sw.matches || !sw.matches("input[data-reveal-url]")) return;
    var box = sw.closest("form") || sw.closest("dialog") || document;
    var fields = function (data) {
      return Object.keys(data).map(function (n) { return [box.querySelector('[name="' + n + '"]'), data[n]]; })
        .filter(function (x) { return x[0]; });
    };
    if (!sw.checked) {
      box.querySelectorAll("[data-revealed]").forEach(function (el) {
        if (el.value === el.dataset.revealed) el.value = "";
        if (el.tagName === "INPUT") el.type = "password";
        delete el.dataset.revealed;
      });
      return;
    }
    var csrf = box.querySelector("input[name=csrfmiddlewaretoken]") || document.querySelector("input[name=csrfmiddlewaretoken]");
    fetch(sw.dataset.revealUrl, {method: "POST", credentials: "same-origin",
                                 headers: {"X-CSRFToken": csrf ? csrf.value : "", "X-Requested-With": "fetch"}})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        fields(data).forEach(function (x) {
          var el = x[0];
          if (!el.value) { el.value = x[1]; el.dataset.revealed = x[1]; }
          if (el.tagName === "INPUT") el.type = "text";
        });
      })
      .catch(function () { sw.checked = false; });
  });
})();
/* Copy buttons: button[data-copy-text] copies its text; button[data-copy-url][data-copy-name] asks the server for a
   kept secret (logged) and copies it without showing it. */
(function () {
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("button.copy-btn");
    if (!b) return;
    var done = function () { var t = b.textContent; b.textContent = "✓"; setTimeout(function () { b.textContent = t; }, 1500); };
    var put = function (v) {
      if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(v).then(done);
      var ta = document.createElement("textarea"); ta.value = v; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); done(); } finally { ta.remove(); }
    };
    if (b.dataset.copyText !== undefined) { put(b.dataset.copyText); return; }
    var csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
    fetch(b.dataset.copyUrl, {method: "POST", credentials: "same-origin",
                              headers: {"X-CSRFToken": csrf ? csrf.value : "", "X-Requested-With": "fetch"}})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { if (d[b.dataset.copyName] !== undefined) put(d[b.dataset.copyName]); });
  });
})();
/* Show the password while typing (correction 11): an eye button next to every password field, also in dialogs and
   in details loaded later. */
(function () {
  function add(i) {
    if (i.dataset.pwEye || i.type !== "password") return;
    i.dataset.pwEye = "1";
    var wrap = document.createElement("span"); wrap.className = "pw-wrap";
    i.parentNode.insertBefore(wrap, i); wrap.appendChild(i);
    var b = document.createElement("button"); b.type = "button"; b.className = "pw-eye"; b.tabIndex = -1;
    var show = document.documentElement.lang === "ru" ? "Показать пароль" : document.documentElement.lang === "kk" ? "Құпиясөзді көрсету" : "Show the password";
    var hide = document.documentElement.lang === "ru" ? "Скрыть пароль" : document.documentElement.lang === "kk" ? "Құпиясөзді жасыру" : "Hide the password";
    b.title = show; b.setAttribute("aria-label", show); b.textContent = "👁";
    b.addEventListener("click", function () {
      var on = i.type === "password";
      i.type = on ? "text" : "password";
      b.classList.toggle("on", on); b.title = on ? hide : show; b.setAttribute("aria-label", b.title);
      i.focus();
    });
    wrap.appendChild(b);
  }
  function scan(root) { (root.querySelectorAll ? root : document).querySelectorAll('input[type="password"]').forEach(add); }
  function start() {
    scan(document);
    new MutationObserver(function (ms) { ms.forEach(function (m) { m.addedNodes.forEach(function (n) { if (n.nodeType === 1) { if (n.matches && n.matches('input[type="password"]')) add(n); else scan(n); } }); }); })
      .observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
/* Copy buttons (correction 15, global rule): next to every value shown as code (names, addresses, user names, secrets
   shown) and next to the inputs that hold such values in forms — in pages, dialogs and details loaded later. */
(function () {
  var NAME = /(^|[-_])(ip|ips|host|address|cidr|subnet|prefix|user|username|login|email|phone|password|name|key|port|vlan|vni|url)([-_]|$)/i;
  var lang = document.documentElement.lang, TITLE = lang === "ru" ? "Копировать" : lang === "kk" ? "Көшіру" : "Copy";
  // owner 2026-10-29 21:06: a Copy button (as in Secrets) for fields and secrets; the ⧉ icon for addresses and other
  // values shown as text, mostly in tables, to save space
  function copy(text, b) {
    var was = b.textContent;
    var done = function () { b.classList.add("done"); b.textContent = "✓"; setTimeout(function () { b.classList.remove("done"); b.textContent = was; }, 1200); };
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done, function () {}); return; }
    var t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
    document.body.appendChild(t); t.select(); try { document.execCommand("copy"); done(); } catch (e) {} t.remove();
  }
  function button(get) {  // u03: one small icon everywhere, right after its value
    var b = document.createElement("button"); b.type = "button"; b.tabIndex = -1;
    b.className = "copy-btn";
    b.title = TITLE; b.setAttribute("aria-label", TITLE); b.textContent = "⧉";
    b.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); var v = get(); if (v) copy(v, b); });
    return b;
  }
  function skip(el) { return el.dataset.copyBtn || el.closest("[data-no-copy], thead, .olist-tools, button, a.btn, select, option"); }
  function addCode(el) {
    if (skip(el)) return;
    var v = (el.textContent || "").trim();
    if (!v || v === "—" || v.length > 400) return;
    el.dataset.copyBtn = "1";
    var next = el.nextElementSibling;  // a Copy button of the page's own already follows it
    if (next && next.classList.contains("copy-btn") && /^\s*$/.test(el.nextSibling && el.nextSibling.nodeType === 3 ? el.nextSibling.textContent : "")) return;
    var b = button(function () { return (el.textContent || "").trim(); });
    if (v.length <= 60 && el.parentNode && !el.closest("pre")) {  // the icon never alone on the next line
      var pair = document.createElement("span"); pair.className = "copy-pair";
      el.parentNode.insertBefore(pair, el); pair.appendChild(el); pair.appendChild(b);
    } else { el.after(b); }
  }
  function addInput(i) {
    if (skip(i) || i.readOnly && !i.value || i.type === "hidden" || i.type === "search" || i.type === "checkbox" || i.type === "radio") return;
    if (!NAME.test(i.name || "")) return;
    i.dataset.copyBtn = "1";
    var at = i.parentNode && i.parentNode.classList.contains("pw-wrap") ? i.parentNode : i;
    // owner 2026-10-29 21:04: the copy button right after the field, on its line (the field and the button in one row)
    var line = document.createElement("span"); line.className = "field-line";
    at.parentNode.insertBefore(line, at); line.appendChild(at);
    line.appendChild(button(function () { return i.value; }));
  }
  function scan(root) {
    if (!root.querySelectorAll) return;
    root.querySelectorAll(".code, [data-copy]").forEach(addCode);
    root.querySelectorAll("form input").forEach(addInput);
    if (root.matches && root.matches(".code, [data-copy]")) addCode(root);
  }
  function start() {
    scan(document);
    new MutationObserver(function (ms) { ms.forEach(function (m) { m.addedNodes.forEach(function (n) { if (n.nodeType === 1 && !n.classList.contains("copy-btn")) scan(n); }); }); })
      .observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
