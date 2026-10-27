(function () {
  "use strict";

  var MAX_NOTE_LENGTH = 500;
  var STATUS_LABELS = {
    measured: "実測",
    skipped: "未実施",
    unreachable: "到達不可",
    meter_fault: "メーター故障",
  };
  var PHASE_LABELS = { lag: "遅れ (lag)", lead: "進み (lead)", unity: "1 (unity)" };
  var CATEGORY_ORDER = { electricity: 0, gas: 1, water: 2 };
  var DAILY_CYCLES = { daily_1x: true, daily_2x: true, daily_3x: true };
  var NONDAILY_TAB = "nondaily";
  var ALL_TAB = "all";
  var UNSET_LOCATION = "__unset__";
  var PF_SCHEMA = "power_factor_percent_phase";

  var app = document.getElementById("app");
  var sheet;
  try {
    sheet = JSON.parse(document.getElementById("mrm-sheet-data").textContent);
  } catch (error) {
    app.textContent = "検針シートのデータを読み込めませんでした。";
    return;
  }
  if (!sheet || sheet.kind !== "mrm-meter-reading-sheet" || sheet.v !== 1) {
    app.textContent = "この検針シートの版数には対応していません。";
    return;
  }

  // ---- BigInt 固定小数点(業務計算に Number を使わない) ----

  var DECIMAL_PATTERN = /^\d+(\.\d+)?$/;
  var TEN = BigInt(10);
  var ZERO = BigInt(0);

  function parseDecimal(text) {
    if (typeof text !== "string" || !DECIMAL_PATTERN.test(text)) {
      return null;
    }
    var dot = text.indexOf(".");
    var scale = dot < 0 ? 0 : text.length - dot - 1;
    var digits = dot < 0 ? text : text.slice(0, dot) + text.slice(dot + 1);
    return { n: BigInt(digits), s: scale };
  }

  function rescale(value, scale) {
    return value.n * TEN ** BigInt(scale - value.s);
  }

  function addDecimal(left, right) {
    var scale = Math.max(left.s, right.s);
    return { n: rescale(left, scale) + rescale(right, scale), s: scale };
  }

  function subtractDecimal(left, right) {
    var scale = Math.max(left.s, right.s);
    return { n: rescale(left, scale) - rescale(right, scale), s: scale };
  }

  function multiplyDecimal(left, right) {
    return { n: left.n * right.n, s: left.s + right.s };
  }

  function compareDecimal(left, right) {
    var scale = Math.max(left.s, right.s);
    var a = rescale(left, scale);
    var b = rescale(right, scale);
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function formatDecimal(value) {
    var negative = value.n < ZERO;
    var digits = (negative ? -value.n : value.n).toString();
    if (value.s > 0) {
      while (digits.length <= value.s) {
        digits = "0" + digits;
      }
      var split = digits.length - value.s;
      digits = digits.slice(0, split) + "." + digits.slice(split);
    }
    return (negative ? "-" : "") + digits;
  }

  function decimalPlaces(text) {
    var dot = text.indexOf(".");
    return dot < 0 ? 0 : text.length - dot - 1;
  }

  var HUNDRED = parseDecimal("100");

  // ---- データ ----

  var definitions = {};
  sheet.definitions.forEach(function (definition) {
    definitions[definition.measurement_definition_id] = definition;
  });
  var meters = {};
  sheet.meters.forEach(function (meter) {
    meters[meter.meter_id] = meter;
  });
  var locationNames = {};
  sheet.locations.forEach(function (location) {
    locationNames[location.location_id] = location.location_name;
  });

  function nowIso() {
    return new Date().toISOString();
  }

  function compareKey(leftTime, leftId, rightTime, rightId) {
    if (leftTime !== rightTime) {
      return leftTime < rightTime ? -1 : 1;
    }
    if (leftId !== rightId) {
      return leftId < rightId ? -1 : 1;
    }
    return 0;
  }

  var slots = sheet.slots.map(function (base) {
    var definition = definitions[base.measurement_definition_id];
    var locationKey = definition.location_id ? definition.location_id : UNSET_LOCATION;
    return {
      id: base.planned_reading_id,
      base: base,
      definition: definition,
      meter: meters[base.meter_id],
      locationKey: locationKey,
      tabKey: DAILY_CYCLES[base.reading_cycle] ? "s:" + base.reading_session : NONDAILY_TAB,
      state: {
        result_status: "measured",
        raw: "",
        pf_percent: "",
        pf_phase: "",
        note: "",
        confirmed: false,
        bad_input: false,
        recorded_at: null,
        client_updated_at: null,
      },
      ui: null,
    };
  });

  var chains = {};
  slots.forEach(function (slot) {
    var key = slot.base.measurement_definition_id + "\u0000" + slot.base.meter_id;
    (chains[key] = chains[key] || []).push(slot);
  });
  Object.keys(chains).forEach(function (key) {
    chains[key].sort(function (a, b) {
      return compareKey(a.base.scheduled_at, a.id, b.base.scheduled_at, b.id);
    });
    chains[key].forEach(function (slot, index) {
      slot.chain = chains[key];
      slot.chainIndex = index;
    });
  });

  // ---- 検証 ----

  function validateSlot(slot) {
    var state = slot.state;
    var errors = {};
    var complete = true;

    var noteLength = Array.from(state.note.trim()).length;
    if (noteLength > MAX_NOTE_LENGTH) {
      errors.note =
        "備考は " + MAX_NOTE_LENGTH + " 文字以内で入力してください (現在 " + noteLength + " 文字)。";
    }

    if (state.result_status === "measured") {
      if (slot.definition.value_schema === PF_SCHEMA) {
        complete = validatePowerFactor(state, errors) && complete;
      } else {
        complete = validateReading(slot, errors) && complete;
      }
    }
    return { errors: errors, complete: complete && Object.keys(errors).length === 0 };
  }

  function validateReading(slot, errors) {
    var text = slot.state.raw.trim();
    if (text === "") {
      return false;
    }
    var value = parseDecimal(text);
    if (!value) {
      errors.raw = "0 以上の数値を、符号・指数・カンマなしで入力してください。";
      return false;
    }
    var places = slot.definition.decimal_places;
    if (decimalPlaces(text) > places) {
      errors.raw = "小数は " + places + " 桁以内で入力してください。";
      return false;
    }
    if (slot.definition.rollover_value !== null) {
      var rollover = parseDecimal(slot.definition.rollover_value);
      if (rollover && compareDecimal(value, rollover) >= 0) {
        errors.raw =
          "ロールオーバー値 " + slot.definition.rollover_value + " 未満で入力してください。";
        return false;
      }
    }
    return true;
  }

  function validatePowerFactor(state, errors) {
    var complete = true;
    var text = state.pf_percent.trim();
    var percent = null;
    if (text === "") {
      complete = false;
    } else {
      percent = parseDecimal(text);
      if (!percent) {
        errors.pf_percent = "力率 (%) は 0〜100 の数値で入力してください。";
        complete = false;
      } else if (compareDecimal(percent, HUNDRED) > 0) {
        errors.pf_percent = "力率 (%) は 100 以下で入力してください。";
        complete = false;
      }
    }
    var isHundred = percent !== null && compareDecimal(percent, HUNDRED) === 0;
    if (state.pf_phase === "") {
      complete = false;
    } else if (percent !== null && !errors.pf_percent) {
      if (isHundred && state.pf_phase !== "unity") {
        errors.pf_phase = "100% のときは位相を unity にしてください。";
        complete = false;
      } else if (!isHundred && state.pf_phase === "unity") {
        errors.pf_phase = "unity は 100% のときだけ選べます。位相を選び直してください。";
        complete = false;
      }
    }
    return complete;
  }

  // ---- 前回値の決定(DB 由来の初期値と同日先行枠の確認済み実測) ----

  function isConfirmedMeasured(slot) {
    return (
      slot.state.confirmed &&
      slot.state.result_status === "measured" &&
      slot.definition.value_schema !== PF_SCHEMA &&
      validateSlot(slot).complete
    );
  }

  function resolvePrevious(slot) {
    var base = slot.base;
    var carried = null;
    for (var index = slot.chainIndex - 1; index >= 0; index -= 1) {
      if (isConfirmedMeasured(slot.chain[index])) {
        carried = slot.chain[index];
        break;
      }
    }
    var hasDb = base.previous_source === "measured" && base.initial_last_reading !== null;
    if (carried && hasDb) {
      var order = compareKey(
        carried.base.scheduled_at,
        carried.id,
        base.initial_previous_scheduled_at,
        base.initial_previous_planned_reading_id
      );
      if (order < 0) {
        carried = null;
      }
    }
    if (carried) {
      return {
        kind: "carried",
        reading: carried.state.raw.trim(),
        fromSlotId: carried.id,
        fromSession: carried.base.reading_session,
      };
    }
    return {
      kind: base.previous_source,
      reading: base.initial_last_reading,
      fromSlotId: null,
      fromSession: null,
    };
  }

  function previousKey(previous) {
    return [previous.kind, previous.reading, previous.fromSlotId].join("\u0000");
  }

  // ---- 参考使用量 ----

  function calculateUsage(slot, previous) {
    var result = { text: null, warnings: [], notes: [], unavailable: null };
    var definition = slot.definition;
    var raw = parseDecimal(slot.state.raw.trim());
    if (
      definition.value_schema === PF_SCHEMA ||
      definition.calculation_method === "raw_only" ||
      slot.state.result_status !== "measured" ||
      !raw ||
      !validateSlot(slot).complete
    ) {
      return result;
    }
    var multiplier = parseDecimal(definition.multiplier);
    var last = previous.reading === null ? null : parseDecimal(previous.reading);
    var usage;
    if (last === null) {
      usage = { n: ZERO, s: 0 };
    } else {
      var delta = subtractDecimal(raw, last);
      if (delta.n < ZERO) {
        if (definition.rollover_value === null) {
          result.unavailable = "前回値 " + previous.reading + " より今回値が小さいため計算できません。";
          result.warnings.push(
            "今回値が前回値 " +
              previous.reading +
              " より小さくなっています (ロールオーバー値が未設定です)。" +
              "実数のまま記録できますが、PC での取込時に拒否されます。"
          );
          return result;
        }
        var corrected = addDecimal(
          subtractDecimal(parseDecimal(definition.rollover_value), last),
          raw
        );
        if (corrected.n < ZERO) {
          result.unavailable = "ロールオーバー補正後も使用量が負になります。";
          result.warnings.push(
            "ロールオーバー補正後も使用量が負になります。" +
              "実数のまま記録できますが、PC での取込時に拒否されます。"
          );
          return result;
        }
        result.notes.push("ロールオーバー値 " + definition.rollover_value + " で補正しました。");
        delta = corrected;
      }
      usage = multiplyDecimal(delta, multiplier);
    }
    result.text = formatDecimal(usage);
    if (definition.warning_threshold !== null) {
      var threshold = parseDecimal(definition.warning_threshold);
      if (threshold && compareDecimal(usage, threshold) > 0) {
        result.warnings.push(
          "参考使用量が警告閾値 " + definition.warning_threshold + " を超えています。"
        );
      }
    }
    return result;
  }

  // ---- 状態の更新 ----

  function snapshotPrevious() {
    var snapshot = {};
    slots.forEach(function (slot) {
      snapshot[slot.id] = previousKey(resolvePrevious(slot));
    });
    return snapshot;
  }

  // 前回値の決定元が変わった確認済み枠は、再確認が必要なため確認を解除する。
  function releaseAffectedConfirmations(before) {
    var changed = true;
    while (changed) {
      changed = false;
      var after = snapshotPrevious();
      slots.forEach(function (slot) {
        if (slot.state.confirmed && before[slot.id] !== after[slot.id]) {
          slot.state.confirmed = false;
          slot.state.client_updated_at = nowIso();
          changed = true;
        }
      });
      before = after;
    }
  }

  function mutate(slot, change) {
    var before = snapshotPrevious();
    change(slot.state);
    slot.state.client_updated_at = nowIso();
    slot.state.bad_input = Object.keys(validateSlot(slot).errors).length > 0;
    releaseAffectedConfirmations(before);
    refresh();
  }

  function editContent(slot, change) {
    mutate(slot, function (state) {
      state.confirmed = false;
      change(state);
    });
  }

  function toggleConfirmation(slot) {
    mutate(slot, function (state) {
      if (state.confirmed) {
        state.confirmed = false;
      } else if (validateSlot(slot).complete) {
        state.confirmed = true;
        state.recorded_at = nowIso();
      }
    });
  }

  // ---- 画面 ----

  var view = { tab: ALL_TAB, location: "", unenteredOnly: false };
  var tabsBar;
  var tabsList;
  var locationSelect;
  var unenteredToggle;
  var summary;
  var emptyMessage;
  var sections = {};
  var tabButtons = {};

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function categoryRank(definition) {
    return Object.prototype.hasOwnProperty.call(CATEGORY_ORDER, definition.category)
      ? CATEGORY_ORDER[definition.category]
      : 3;
  }

  function compareSlotsForDisplay(a, b) {
    var rank = categoryRank(a.definition) - categoryRank(b.definition);
    if (rank !== 0) {
      return rank;
    }
    if (a.definition.display_name !== b.definition.display_name) {
      return a.definition.display_name < b.definition.display_name ? -1 : 1;
    }
    return compareKey(a.base.scheduled_at, a.id, b.base.scheduled_at, b.id);
  }

  function locationLabel(locationKey) {
    if (locationKey === UNSET_LOCATION) {
      return "場所未設定";
    }
    return locationNames[locationKey] || locationKey;
  }

  function textInput(className) {
    var input = document.createElement("input");
    input.type = "text";
    input.className = className;
    input.inputMode = "decimal";
    input.autocomplete = "off";
    return input;
  }

  function buildCard(slot) {
    var ui = {};
    var card = element("article", "card");
    card.setAttribute("data-slot-id", slot.id);
    ui.card = card;

    var head = element("div", "card-head");
    head.appendChild(element("h3", "card-title", slot.definition.display_name));
    head.appendChild(element("span", "card-session", slot.base.reading_session));
    ui.stateBadge = element("span", "card-state");
    head.appendChild(ui.stateBadge);
    card.appendChild(head);

    var meterText = "メーター: " + slot.meter.meter_name;
    if (slot.meter.meter_serial) {
      meterText += " (" + slot.meter.meter_serial + ")";
    }
    meterText += " / 単位: " + slot.definition.unit;
    if (slot.definition.value_schema !== PF_SCHEMA) {
      meterText += " / 倍率: " + slot.definition.multiplier;
    }
    card.appendChild(element("p", "card-meta", meterText));
    if (slot.base.task_state === "auto_closed") {
      card.appendChild(
        element("p", "card-closed", "自動クローズ済みの枠です。入力すると完了として扱われます。")
      );
    }
    ui.previous = element("p", "previous");
    card.appendChild(ui.previous);

    var statusLabel = element("label", "field");
    statusLabel.appendChild(element("span", "field-label", "状態"));
    ui.status = document.createElement("select");
    ui.status.className = "status";
    Object.keys(STATUS_LABELS).forEach(function (value) {
      var option = element("option", "", STATUS_LABELS[value]);
      option.value = value;
      ui.status.appendChild(option);
    });
    statusLabel.appendChild(ui.status);
    card.appendChild(statusLabel);

    ui.readingField = element("div", "field reading-field");
    ui.pfField = element("div", "field pf-field");
    if (slot.definition.value_schema === PF_SCHEMA) {
      var percentLabel = element("label", "field-row");
      percentLabel.appendChild(element("span", "field-label", "力率 (%)"));
      ui.pfPercent = textInput("pf-percent");
      percentLabel.appendChild(ui.pfPercent);
      ui.pfField.appendChild(percentLabel);
      ui.pfPercentError = element("p", "field-error");
      ui.pfField.appendChild(ui.pfPercentError);
      var phaseLabel = element("label", "field-row");
      phaseLabel.appendChild(element("span", "field-label", "位相"));
      ui.pfPhase = document.createElement("select");
      ui.pfPhase.className = "pf-phase";
      var blank = element("option", "", "選択してください");
      blank.value = "";
      ui.pfPhase.appendChild(blank);
      ["lag", "lead", "unity"].forEach(function (value) {
        var option = element("option", "", PHASE_LABELS[value]);
        option.value = value;
        ui.pfPhase.appendChild(option);
      });
      phaseLabel.appendChild(ui.pfPhase);
      ui.pfField.appendChild(phaseLabel);
      ui.pfPhaseError = element("p", "field-error");
      ui.pfField.appendChild(ui.pfPhaseError);
      card.appendChild(ui.pfField);
    } else {
      var rawLabel = element("label", "field-row");
      rawLabel.appendChild(element("span", "field-label", "今回値"));
      ui.raw = textInput("raw");
      rawLabel.appendChild(ui.raw);
      rawLabel.appendChild(element("span", "unit", slot.definition.unit));
      ui.readingField.appendChild(rawLabel);
      ui.rawError = element("p", "field-error");
      ui.readingField.appendChild(ui.rawError);
      card.appendChild(ui.readingField);
    }

    ui.usage = element("p", "usage");
    card.appendChild(ui.usage);
    ui.messages = element("ul", "messages");
    card.appendChild(ui.messages);

    var noteLabel = element("label", "field");
    noteLabel.appendChild(element("span", "field-label", "備考"));
    ui.note = document.createElement("textarea");
    ui.note.className = "note";
    ui.note.rows = 2;
    noteLabel.appendChild(ui.note);
    card.appendChild(noteLabel);
    ui.noteError = element("p", "field-error");
    card.appendChild(ui.noteError);

    var actions = element("div", "actions");
    ui.confirm = element("button", "confirm");
    ui.confirm.type = "button";
    ui.next = element("button", "next", "次へ");
    ui.next.type = "button";
    actions.appendChild(ui.confirm);
    actions.appendChild(ui.next);
    card.appendChild(actions);
    ui.nextMessage = element("p", "next-message");
    card.appendChild(ui.nextMessage);

    bindCard(slot, ui);
    return ui;
  }

  function bindCard(slot, ui) {
    ui.status.addEventListener("change", function () {
      editContent(slot, function (state) {
        state.result_status = ui.status.value;
      });
    });
    if (ui.raw) {
      ui.raw.addEventListener("input", function () {
        editContent(slot, function (state) {
          state.raw = ui.raw.value;
        });
      });
    }
    if (ui.pfPercent) {
      ui.pfPercent.addEventListener("input", function () {
        editContent(slot, function (state) {
          state.pf_percent = ui.pfPercent.value;
          var percent = parseDecimal(state.pf_percent.trim());
          if (percent && compareDecimal(percent, HUNDRED) === 0) {
            state.pf_phase = "unity";
          } else if (state.pf_phase === "unity") {
            state.pf_phase = "";
          }
        });
      });
      ui.pfPhase.addEventListener("change", function () {
        editContent(slot, function (state) {
          state.pf_phase = ui.pfPhase.value;
        });
      });
    }
    ui.note.addEventListener("input", function () {
      editContent(slot, function (state) {
        state.note = ui.note.value;
      });
    });
    ui.confirm.addEventListener("click", function () {
      toggleConfirmation(slot);
    });
    ui.next.addEventListener("click", function () {
      goToNext(slot);
    });
  }

  function setValue(control, value) {
    if (control.value !== value) {
      control.value = value;
    }
  }

  function previousText(slot, previous) {
    var unit = slot.definition.unit;
    switch (previous.kind) {
      case "carried":
        return (
          "前回値: " +
          previous.reading +
          " " +
          unit +
          " (同日 " +
          previous.fromSession +
          " の入力値を引き継ぎ)"
        );
      case "measured":
        return "前回値: " + previous.reading + " " + unit;
      case "replacement_initial":
        return "前回値: " + previous.reading + " " + unit + " (メーター交換時の初期値)";
      case "replacement_no_initial":
        return "前回値なし (メーター交換後の初回のため使用量は 0)";
      default:
        return "前回値なし (使用量は 0)";
    }
  }

  function updateCard(slot) {
    var ui = slot.ui;
    var state = slot.state;
    var validation = validateSlot(slot);
    var isPf = slot.definition.value_schema === PF_SCHEMA;
    var measured = state.result_status === "measured";

    ui.card.setAttribute("data-confirmed", state.confirmed ? "true" : "false");
    ui.card.setAttribute("data-status", state.result_status);
    ui.card.classList.toggle("confirmed", state.confirmed);
    ui.stateBadge.textContent = state.confirmed ? "確認済み" : "未入力";
    setValue(ui.status, state.result_status);
    setValue(ui.note, state.note);
    ui.noteError.textContent = validation.errors.note || "";

    var usage = { text: null, warnings: [], notes: [], unavailable: null };
    if (isPf) {
      ui.previous.hidden = true;
      ui.usage.hidden = true;
      ui.readingField.hidden = true;
      ui.pfField.hidden = !measured;
      setValue(ui.pfPercent, state.pf_percent);
      setValue(ui.pfPhase, state.pf_phase);
      var percent = parseDecimal(state.pf_percent.trim());
      var isHundred = percent !== null && compareDecimal(percent, HUNDRED) === 0;
      Array.prototype.forEach.call(ui.pfPhase.options, function (option) {
        if (option.value === "unity") {
          option.disabled = !isHundred;
        } else if (option.value !== "") {
          option.disabled = isHundred;
        }
      });
      ui.pfPhase.disabled = isHundred;
      ui.pfPercentError.textContent = validation.errors.pf_percent || "";
      ui.pfPhaseError.textContent = validation.errors.pf_phase || "";
    } else {
      var previous = resolvePrevious(slot);
      ui.pfField.hidden = true;
      ui.readingField.hidden = !measured;
      setValue(ui.raw, state.raw);
      ui.rawError.textContent = validation.errors.raw || "";
      ui.previous.hidden = false;
      ui.previous.textContent = previousText(slot, previous);
      ui.card.setAttribute("data-previous", previous.reading === null ? "" : previous.reading);
      ui.card.setAttribute("data-previous-kind", previous.kind);
      usage = calculateUsage(slot, previous);
      ui.usage.hidden = !measured || slot.definition.calculation_method === "raw_only";
      if (usage.text !== null) {
        ui.usage.textContent = "参考使用量: " + usage.text + " " + slot.definition.unit;
      } else {
        ui.usage.textContent = "参考使用量: " + (usage.unavailable || "—");
      }
      ui.card.setAttribute("data-usage", usage.text === null ? "" : usage.text);
    }

    ui.messages.textContent = "";
    usage.warnings.forEach(function (message) {
      ui.messages.appendChild(element("li", "warning", message));
    });
    usage.notes.forEach(function (message) {
      ui.messages.appendChild(element("li", "info", message));
    });

    ui.confirm.textContent = state.confirmed ? "確認を解除" : "確認";
    ui.confirm.disabled = !state.confirmed && !validation.complete;
    ui.card.classList.toggle("has-warning", usage.warnings.length > 0);
  }

  function inTab(slot) {
    return view.tab === ALL_TAB || slot.tabKey === view.tab;
  }

  function isVisible(slot) {
    if (!inTab(slot)) {
      return false;
    }
    if (view.location !== "" && slot.locationKey !== view.location) {
      return false;
    }
    return !(view.unenteredOnly && slot.state.confirmed);
  }

  function tabDefinitions() {
    var sessions = {};
    var hasNonDaily = false;
    slots.forEach(function (slot) {
      if (slot.tabKey === NONDAILY_TAB) {
        hasNonDaily = true;
      } else {
        sessions[slot.base.reading_session] = true;
      }
    });
    var list = [{ key: ALL_TAB, label: "すべて" }];
    Object.keys(sessions)
      .sort()
      .forEach(function (session) {
        list.push({ key: "s:" + session, label: session });
      });
    if (hasNonDaily) {
      list.push({ key: NONDAILY_TAB, label: "週次・月次・隔月" });
    }
    return list;
  }

  function countFor(tabKey) {
    var total = 0;
    var done = 0;
    slots.forEach(function (slot) {
      if (tabKey === ALL_TAB || slot.tabKey === tabKey) {
        total += 1;
        if (slot.state.confirmed) {
          done += 1;
        }
      }
    });
    return { total: total, done: done };
  }

  // 未消化の枠が残る最も早い日次検針枠、なければ非日次、すべて消化済みなら「すべて」。
  function initialTab() {
    var list = tabDefinitions();
    for (var index = 1; index < list.length; index += 1) {
      var count = countFor(list[index].key);
      if (list[index].key !== NONDAILY_TAB && count.done < count.total) {
        return list[index].key;
      }
    }
    var nonDaily = countFor(NONDAILY_TAB);
    return nonDaily.done < nonDaily.total ? NONDAILY_TAB : ALL_TAB;
  }

  function updateTabs() {
    tabDefinitions().forEach(function (tab) {
      var button = tabButtons[tab.key];
      var count = countFor(tab.key);
      button.textContent = tab.label + " (" + count.done + "/" + count.total + ")";
      var selected = tab.key === view.tab;
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.classList.toggle("selected", selected);
    });
  }

  function updateLocationOptions() {
    var present = {};
    slots.forEach(function (slot) {
      if (inTab(slot)) {
        present[slot.locationKey] = true;
      }
    });
    if (view.location !== "" && !present[view.location]) {
      view.location = "";
    }
    var keys = Object.keys(sections).filter(function (key) {
      return present[key];
    });
    var signature = keys.join("\u0000");
    if (locationSelect.getAttribute("data-signature") !== signature) {
      locationSelect.setAttribute("data-signature", signature);
      locationSelect.textContent = "";
      var all = element("option", "", "すべての場所");
      all.value = "";
      locationSelect.appendChild(all);
      keys.forEach(function (key) {
        var option = element("option", "", locationLabel(key));
        option.value = key;
        locationSelect.appendChild(option);
      });
    }
    locationSelect.value = view.location;
  }

  function updateSections() {
    var anyVisible = false;
    Object.keys(sections).forEach(function (key) {
      var section = sections[key];
      var scoped = section.slots.filter(inTab);
      var visibleCount = 0;
      section.slots.forEach(function (slot) {
        var visible = isVisible(slot);
        slot.ui.card.hidden = !visible;
        if (visible) {
          visibleCount += 1;
        }
      });
      var done = scoped.filter(function (slot) {
        return slot.state.confirmed;
      }).length;
      section.progress.textContent = done + "/" + scoped.length;
      var complete = scoped.length > 0 && done === scoped.length;
      section.complete.hidden = !complete;
      section.node.classList.toggle("complete", complete);
      section.node.hidden = visibleCount === 0;
      if (visibleCount > 0) {
        anyVisible = true;
      }
    });
    emptyMessage.hidden = anyVisible;
  }

  function updateSummary() {
    var count = countFor(ALL_TAB);
    summary.textContent = "確認済み " + count.done + " / " + count.total + " 件";
  }

  function updateStickyOffset() {
    document.documentElement.style.setProperty("--tabs-height", tabsBar.offsetHeight + "px");
  }

  function refresh() {
    slots.forEach(updateCard);
    updateTabs();
    updateLocationOptions();
    updateSections();
    updateSummary();
    updateStickyOffset();
  }

  function setSectionCollapsed(section, collapsed) {
    section.node.classList.toggle("collapsed", collapsed);
    section.toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }

  function focusSlot(slot) {
    setSectionCollapsed(sections[slot.locationKey], false);
    var target = slot.ui.status;
    if (slot.state.result_status === "measured") {
      target = slot.ui.raw || slot.ui.pfPercent;
    }
    slot.ui.card.scrollIntoView({ block: "center" });
    target.focus({ preventScroll: true });
  }

  function goToNext(slot) {
    var list = sections[slot.locationKey].slots;
    var start = list.indexOf(slot);
    var ordered = list.slice(start + 1).concat(list.slice(0, start));
    var target = null;
    for (var index = 0; index < ordered.length; index += 1) {
      if (!ordered[index].state.confirmed && isVisible(ordered[index])) {
        target = ordered[index];
        break;
      }
    }
    slots.forEach(function (other) {
      other.ui.nextMessage.textContent = "";
    });
    if (!target) {
      slot.ui.nextMessage.textContent = "この場所に未入力の枠はありません。";
      return;
    }
    focusSlot(target);
  }

  function centerTab(key) {
    var button = tabButtons[key];
    var left = button.offsetLeft - (tabsList.clientWidth - button.offsetWidth) / 2;
    tabsList.scrollLeft = Math.max(0, left);
  }

  function selectTab(key) {
    view.tab = key;
    refresh();
    centerTab(key);
  }

  function buildHeader() {
    var header = element("header", "sheet-header");
    header.appendChild(element("h1", "", "検針シート " + sheet.business_date));
    header.appendChild(
      element(
        "p",
        "sheet-meta",
        "出力日時 " + sheet.generated_at_local + " / 検針枠 " + sheet.slots.length + " 件"
      )
    );
    summary = element("p", "sheet-summary");
    header.appendChild(summary);
    app.appendChild(header);
  }

  function buildNavigation() {
    tabsBar = element("nav", "tabs-bar");
    tabsBar.setAttribute("aria-label", "検針時間・周期");
    tabsList = element("div", "tabs");
    tabsList.setAttribute("role", "tablist");
    tabDefinitions().forEach(function (tab) {
      var button = element("button", "tab");
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("data-tab", tab.key);
      button.addEventListener("click", function () {
        selectTab(tab.key);
      });
      tabButtons[tab.key] = button;
      tabsList.appendChild(button);
    });
    tabsBar.appendChild(tabsList);
    app.appendChild(tabsBar);

    var toolbar = element("div", "toolbar");
    var locationItem = element("label", "toolbar-item");
    locationItem.appendChild(element("span", "", "場所"));
    locationSelect = document.createElement("select");
    locationSelect.className = "location-select";
    locationSelect.addEventListener("change", function () {
      view.location = locationSelect.value;
      refresh();
    });
    locationItem.appendChild(locationSelect);
    toolbar.appendChild(locationItem);
    var toggleItem = element("label", "toolbar-item toggle");
    unenteredToggle = document.createElement("input");
    unenteredToggle.type = "checkbox";
    unenteredToggle.className = "unentered-toggle";
    unenteredToggle.addEventListener("change", function () {
      view.unenteredOnly = unenteredToggle.checked;
      refresh();
    });
    toggleItem.appendChild(unenteredToggle);
    toggleItem.appendChild(element("span", "", "未入力のみ"));
    toolbar.appendChild(toggleItem);
    app.appendChild(toolbar);
  }

  function compareLocationKeys(a, b) {
    if (a === UNSET_LOCATION || b === UNSET_LOCATION) {
      return a === UNSET_LOCATION ? 1 : -1;
    }
    var nameA = locationLabel(a);
    var nameB = locationLabel(b);
    if (nameA !== nameB) {
      return nameA < nameB ? -1 : 1;
    }
    return a < b ? -1 : 1;
  }

  function buildSection(key) {
    var node = element("section", "location");
    node.setAttribute("data-location-id", key === UNSET_LOCATION ? "" : key);
    var header = element("header", "location-header");
    var toggle = element("button", "location-toggle", locationLabel(key));
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "true");
    var progress = element("span", "location-progress");
    var complete = element("span", "badge complete-badge", "完了");
    header.appendChild(toggle);
    header.appendChild(progress);
    header.appendChild(complete);
    node.appendChild(header);
    var cards = element("div", "cards");
    node.appendChild(cards);
    var section = {
      node: node,
      toggle: toggle,
      progress: progress,
      complete: complete,
      slots: slots
        .filter(function (slot) {
          return slot.locationKey === key;
        })
        .sort(compareSlotsForDisplay),
    };
    toggle.addEventListener("click", function () {
      setSectionCollapsed(section, !node.classList.contains("collapsed"));
    });
    section.slots.forEach(function (slot) {
      slot.ui = buildCard(slot);
      cards.appendChild(slot.ui.card);
    });
    return section;
  }

  function buildSections() {
    var list = element("div", "locations");
    var keys = {};
    slots.forEach(function (slot) {
      keys[slot.locationKey] = true;
    });
    Object.keys(keys)
      .sort(compareLocationKeys)
      .forEach(function (key) {
        sections[key] = buildSection(key);
        list.appendChild(sections[key].node);
      });
    app.appendChild(list);
    emptyMessage = element("p", "empty-message", "表示できる検針枠がありません。");
    app.appendChild(emptyMessage);
  }

  buildHeader();
  buildNavigation();
  buildSections();
  view.tab = initialTab();
  refresh();
  centerTab(view.tab);
  window.addEventListener("resize", updateStickyOffset);
})();
