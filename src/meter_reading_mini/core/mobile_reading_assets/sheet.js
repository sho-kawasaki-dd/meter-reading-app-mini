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
  var DRAFT_KIND = "mrm-meter-reading-draft";
  var RESULT_KIND = "mrm-meter-reading-result";
  var STORAGE_DATABASE_NAME = "mrm_meter_reading";
  var STORAGE_DATABASE_VERSION = 1;
  var STORAGE_TIMEOUT_MS = 5000;

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
    clearResultOutput();
    refresh();
    scheduleDraftSave(false);
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
  var toolbarNode;
  var locationsNode;
  var storageDatabase = null;
  var storageRevision = 0;
  var storageInitialized = false;
  var storageWriteAllowed = false;
  var storageStatusNode;
  var storageMessageNode;
  var storageRecoveryNode;
  var storageRetryButton;
  var storageManualButton;
  var storageReplaceButton;
  var storageConflictNode;
  var storageConflictMessageNode;
  var storageUseStoredButton;
  var storageUseCurrentButton;
  var storageReplaceConfirmationNode;
  var storageConfirmReplaceButton;
  var storageCancelReplaceButton;
  var draftDownloadButton;
  var draftShowButton;
  var draftCopyButton;
  var draftFileInput;
  var draftImportText;
  var draftImportButton;
  var draftFeedbackNode;
  var draftPreviewNode;
  var draftPreviewSummaryNode;
  var draftConfirmImportButton;
  var draftCancelImportButton;
  var draftExportText;
  var resultCopyButton;
  var resultDownloadButton;
  var resultFeedbackNode;
  var resultExportText;
  var resultCountNode;
  var editGeneration = 0;
  var savedGeneration = 0;
  var pendingSave = null;
  var saveQueueActive = false;
  var draftConflictCandidate = null;
  var draftImportCandidate = null;
  var draftReadGeneration = 0;

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
    updateResultTools();
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

  function draftButton(id, label) {
    var button = element("button", "draft-button", label);
    button.id = id;
    button.type = "button";
    return button;
  }

  function setStorageStatus(status, message) {
    storageStatusNode.dataset.status = status;
    storageStatusNode.dataset.editGeneration = String(editGeneration);
    storageStatusNode.dataset.savedGeneration = String(savedGeneration);
    storageStatusNode.textContent = message;
  }

  function setStorageMessage(message) {
    storageMessageNode.textContent = message;
    storageMessageNode.hidden = message.length === 0;
  }

  function setDraftFeedback(message) {
    draftFeedbackNode.textContent = message;
    draftFeedbackNode.hidden = message.length === 0;
  }

  function setMainInputEnabled(enabled) {
    tabsBar.inert = !enabled;
    toolbarNode.inert = !enabled;
    locationsNode.inert = !enabled;
  }

  function setDraftActionsEnabled(enabled) {
    [
      draftDownloadButton,
      draftShowButton,
      draftCopyButton,
      draftFileInput,
      draftImportText,
      draftImportButton,
    ].forEach(function (control) {
      control.disabled = !enabled;
    });
  }

  function setResultFeedback(message) {
    resultFeedbackNode.textContent = message;
    resultFeedbackNode.hidden = message.length === 0;
  }

  function clearResultOutput() {
    if (resultExportText) {
      resultExportText.value = "";
      resultExportText.hidden = true;
    }
    if (resultFeedbackNode) {
      setResultFeedback("");
    }
  }

  function isExportableResult(slot) {
    var state = slot.state;
    return (
      state.confirmed &&
      validateSlot(slot).complete &&
      typeof state.recorded_at === "string" &&
      validIsoTimestamp(state.recorded_at) &&
      typeof state.client_updated_at === "string" &&
      validIsoTimestamp(state.client_updated_at)
    );
  }

  function exportableResults() {
    return slots.filter(isExportableResult);
  }

  function updateResultTools() {
    if (!resultCountNode) {
      return;
    }
    var count = exportableResults().length;
    resultCountNode.textContent =
      count === 0
        ? "確認済みの正常な枠がありません。1 件以上で出力できます。"
        : "出力対象: " + count + " 件 (確認済みの正常な枠のみ)";
    resultCopyButton.disabled = count === 0;
    resultDownloadButton.disabled = count === 0;
  }

  function buildResultTools() {
    var panel = element("section", "result-tools");
    panel.setAttribute("aria-label", "検針結果の出力");
    panel.appendChild(element("h2", "", "検針結果の出力"));
    resultCountNode = element("p", "result-count", "");
    resultCountNode.id = "result-count";
    resultCountNode.setAttribute("role", "status");
    panel.appendChild(resultCountNode);

    var actions = element("div", "result-actions");
    resultCopyButton = draftButton("result-copy", "MR1 をコピー");
    resultCopyButton.addEventListener("click", function () {
      void copyResultCode();
    });
    resultDownloadButton = draftButton("result-download", "結果 JSON を保存");
    resultDownloadButton.addEventListener("click", function () {
      downloadResultJson();
    });
    actions.appendChild(resultCopyButton);
    actions.appendChild(resultDownloadButton);
    panel.appendChild(actions);

    resultExportText = document.createElement("textarea");
    resultExportText.id = "result-export-text";
    resultExportText.className = "draft-json";
    resultExportText.setAttribute("aria-label", "表示した検針結果テキスト");
    resultExportText.readOnly = true;
    resultExportText.hidden = true;
    panel.appendChild(resultExportText);

    resultFeedbackNode = element("p", "result-feedback");
    resultFeedbackNode.id = "result-feedback";
    resultFeedbackNode.setAttribute("role", "status");
    resultFeedbackNode.hidden = true;
    panel.appendChild(resultFeedbackNode);
    app.appendChild(panel);
  }

  function buildDraftTools() {
    var panel = element("section", "draft-tools");
    panel.setAttribute("aria-label", "途中経過の保存と復元");
    panel.appendChild(element("h2", "", "途中経過の保存"));

    storageStatusNode = element("p", "draft-storage-status", "読込中");
    storageStatusNode.id = "draft-storage-status";
    storageStatusNode.dataset.status = "loading";
    storageStatusNode.setAttribute("role", "status");
    storageStatusNode.setAttribute("aria-live", "polite");
    panel.appendChild(storageStatusNode);

    storageMessageNode = element("p", "draft-storage-message");
    storageMessageNode.id = "draft-storage-message";
    storageMessageNode.hidden = true;
    panel.appendChild(storageMessageNode);

    storageRecoveryNode = element("div", "draft-actions");
    storageRetryButton = draftButton("draft-retry", "保存先を再確認");
    storageRetryButton.hidden = true;
    storageRetryButton.addEventListener("click", function () {
      void retryDraftStorage();
    });
    storageManualButton = draftButton("draft-manual-continue", "保存せずに入力を続ける");
    storageManualButton.hidden = true;
    storageManualButton.addEventListener("click", function () {
      storageInitialized = true;
      storageWriteAllowed = false;
      setMainInputEnabled(true);
      setDraftActionsEnabled(true);
      setStorageStatus("error", "保存失敗: JSON で途中経過を退避してください");
      setStorageMessage(
        "IndexedDB の保存内容を確認できません。入力はこの画面に保持されますが、自動保存されません。JSON を保存してください。"
      );
    });
    storageReplaceButton = draftButton(
      "draft-replace",
      "このシートの入力で保存データを置き換える"
    );
    storageReplaceButton.hidden = true;
    storageReplaceButton.addEventListener("click", function () {
      requestDraftReplacement();
    });
    storageRecoveryNode.appendChild(storageRetryButton);
    storageRecoveryNode.appendChild(storageManualButton);
    storageRecoveryNode.appendChild(storageReplaceButton);
    panel.appendChild(storageRecoveryNode);

    storageConflictNode = element("div", "draft-conflict");
    storageConflictNode.id = "draft-conflict";
    storageConflictNode.hidden = true;
    storageConflictMessageNode = element("p", "", "");
    storageConflictNode.appendChild(storageConflictMessageNode);
    storageUseStoredButton = draftButton(
      "draft-use-stored",
      "保存済みの入力を読み込む"
    );
    storageUseStoredButton.addEventListener("click", function () {
      if (draftConflictCandidate) {
        applyDraftStates(draftConflictCandidate.states, draftConflictCandidate.revision);
      }
    });
    storageUseCurrentButton = draftButton(
      "draft-use-current",
      "現在の入力で置き換える"
    );
    storageUseCurrentButton.addEventListener("click", function () {
      requestDraftReplacement();
    });
    storageConflictNode.appendChild(storageUseStoredButton);
    storageConflictNode.appendChild(storageUseCurrentButton);
    panel.appendChild(storageConflictNode);

    storageReplaceConfirmationNode = element("div", "draft-confirmation");
    storageReplaceConfirmationNode.id = "draft-replace-confirmation";
    storageReplaceConfirmationNode.hidden = true;
    storageReplaceConfirmationNode.appendChild(
      element(
        "p",
        "",
        "保存済みデータをこの画面の入力で置き換えます。別タブの入力は復元できなくなります。続行しますか。"
      )
    );
    storageConfirmReplaceButton = draftButton(
      "draft-confirm-replace",
      "置き換えて保存する"
    );
    storageConfirmReplaceButton.addEventListener("click", function () {
      confirmDraftReplacement();
    });
    storageCancelReplaceButton = draftButton("draft-cancel-replace", "キャンセル");
    storageCancelReplaceButton.addEventListener("click", function () {
      storageReplaceConfirmationNode.hidden = true;
    });
    storageReplaceConfirmationNode.appendChild(storageConfirmReplaceButton);
    storageReplaceConfirmationNode.appendChild(storageCancelReplaceButton);
    panel.appendChild(storageReplaceConfirmationNode);

    var backupActions = element("div", "draft-actions");
    draftDownloadButton = draftButton("draft-download", "途中経過 JSON を保存");
    draftDownloadButton.addEventListener("click", function () {
      downloadDraft();
    });
    draftShowButton = draftButton("draft-show", "JSON を表示");
    draftShowButton.addEventListener("click", function () {
      showDraftJson();
    });
    draftCopyButton = draftButton("draft-copy", "JSON をコピー");
    draftCopyButton.addEventListener("click", function () {
      void copyDraftJson();
    });
    backupActions.appendChild(draftDownloadButton);
    backupActions.appendChild(draftShowButton);
    backupActions.appendChild(draftCopyButton);
    panel.appendChild(backupActions);

    draftExportText = document.createElement("textarea");
    draftExportText.id = "draft-export-text";
    draftExportText.className = "draft-json";
    draftExportText.setAttribute("aria-label", "表示した途中経過 JSON");
    draftExportText.readOnly = true;
    draftExportText.hidden = true;
    panel.appendChild(draftExportText);

    var fileLabel = element("label", "draft-file-label", "JSON ファイルから読み込む");
    draftFileInput = document.createElement("input");
    draftFileInput.id = "draft-file";
    draftFileInput.type = "file";
    draftFileInput.accept = ".json,application/json";
    draftFileInput.addEventListener("change", function () {
      var file = draftFileInput.files && draftFileInput.files[0];
      draftFileInput.value = "";
      readDraftFile(file);
    });
    fileLabel.appendChild(draftFileInput);
    panel.appendChild(fileLabel);

    var importLabel = element("label", "draft-import-label", "途中経過 JSON を貼り付け");
    draftImportText = document.createElement("textarea");
    draftImportText.id = "draft-import-text";
    draftImportText.className = "draft-json";
    draftImportText.setAttribute("aria-label", "読み込む途中経過 JSON");
    importLabel.appendChild(draftImportText);
    panel.appendChild(importLabel);
    draftImportButton = draftButton("draft-import", "貼り付けた JSON を検証");
    draftImportButton.addEventListener("click", function () {
      inspectDraftText(draftImportText.value);
    });
    panel.appendChild(draftImportButton);

    draftFeedbackNode = element("p", "draft-feedback");
    draftFeedbackNode.id = "draft-feedback";
    draftFeedbackNode.setAttribute("role", "status");
    draftFeedbackNode.hidden = true;
    panel.appendChild(draftFeedbackNode);

    draftPreviewNode = element("div", "draft-confirmation");
    draftPreviewNode.id = "draft-preview";
    draftPreviewNode.hidden = true;
    draftPreviewSummaryNode = element("p", "", "");
    draftPreviewNode.appendChild(draftPreviewSummaryNode);
    draftConfirmImportButton = draftButton(
      "draft-confirm-import",
      "この途中経過で全体を置き換える"
    );
    draftConfirmImportButton.addEventListener("click", function () {
      confirmDraftImport();
    });
    draftCancelImportButton = draftButton("draft-cancel-import", "キャンセル");
    draftCancelImportButton.addEventListener("click", function () {
      clearDraftImportCandidate();
      setDraftFeedback("読み込みをキャンセルしました。画面と保存データは変更していません。");
    });
    draftPreviewNode.appendChild(draftConfirmImportButton);
    draftPreviewNode.appendChild(draftCancelImportButton);
    panel.appendChild(draftPreviewNode);

    app.appendChild(panel);
    setDraftActionsEnabled(false);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function hasExactKeys(value, keys) {
    if (!isObject(value)) {
      return false;
    }
    var actual = Object.keys(value).sort();
    var expected = keys.slice().sort();
    return (
      actual.length === expected.length &&
      actual.every(function (key, index) {
        return key === expected[index];
      })
    );
  }

  function sameJsonValue(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function validIsoTimestamp(value) {
    return (
      value === null ||
      (typeof value === "string" &&
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(
          value
        ) &&
        !Number.isNaN(Date.parse(value)))
    );
  }

  function createDraft() {
    return {
      kind: DRAFT_KIND,
      v: 1,
      sheet_id: sheet.sheet_id,
      business_date: sheet.business_date,
      generated_at: sheet.generated_at,
      filter: sheet.filter,
      slots: slots.map(function (slot) {
        return {
          planned_reading_id: slot.id,
          measurement_definition_id: slot.base.measurement_definition_id,
          meter_id: slot.base.meter_id,
          initial_last_reading: slot.base.initial_last_reading,
          previous_source: slot.base.previous_source,
          initial_previous_scheduled_at: slot.base.initial_previous_scheduled_at,
          initial_previous_planned_reading_id:
            slot.base.initial_previous_planned_reading_id,
          result_status: slot.state.result_status,
          raw: slot.state.raw,
          pf_percent: slot.state.pf_percent,
          pf_phase: slot.state.pf_phase,
          note: slot.state.note,
          confirmed: slot.state.confirmed,
          bad_input: slot.state.bad_input,
          recorded_at: slot.state.recorded_at,
          client_updated_at: slot.state.client_updated_at,
        };
      }),
    };
  }

  function createResultPayload() {
    var entries = exportableResults().map(function (slot) {
      var state = slot.state;
      var isMeasured = state.result_status === "measured";
      var isPowerFactor = slot.definition.value_schema === PF_SCHEMA;
      return {
        planned_reading_id: slot.id,
        result_status: state.result_status,
        raw_reading:
          isMeasured && !isPowerFactor ? state.raw.trim() : null,
        structured_payload:
          isMeasured && isPowerFactor
            ? { percent: state.pf_percent.trim(), phase: state.pf_phase }
            : null,
        recorded_at: state.recorded_at,
        client_updated_at: state.client_updated_at,
        note: state.note.trim() || null,
      };
    });
    if (entries.length === 0) {
      throw new Error("確認済みの正常な検針結果がありません。");
    }
    return {
      kind: RESULT_KIND,
      v: 1,
      sheet_id: sheet.sheet_id,
      business_date: sheet.business_date,
      generated_at: sheet.generated_at,
      filter: sheet.filter,
      entries: entries,
    };
  }

  function resultCode(payload) {
    var encoded = new TextEncoder().encode(JSON.stringify(payload));
    var binary = "";
    for (var index = 0; index < encoded.length; index += 1) {
      binary += String.fromCharCode(encoded[index]);
    }
    var base64 = btoa(binary);
    var crc = 0xffffffff;
    for (var byteIndex = 0; byteIndex < base64.length; byteIndex += 1) {
      crc ^= base64.charCodeAt(byteIndex);
      for (var bit = 0; bit < 8; bit += 1) {
        crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
      }
    }
    var checksum = ((crc ^ 0xffffffff) >>> 0).toString(16);
    checksum = ("00000000" + checksum).slice(-8);
    return "MR1:" + base64 + "." + checksum + ":END";
  }

  function showResultText(text) {
    resultExportText.value = text;
    resultExportText.hidden = false;
    resultExportText.focus();
    resultExportText.select();
  }

  async function copyResultCode() {
    try {
      var code = resultCode(createResultPayload());
      showResultText(code);
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("この環境ではクリップボードを利用できません。");
      }
      await navigator.clipboard.writeText(code);
      setResultFeedback("MR1 結果コードをクリップボードにコピーしました。");
    } catch (error) {
      if (resultExportText.value) {
        resultExportText.focus();
        resultExportText.select();
      }
      setResultFeedback(
        "コピーできませんでした: " +
          (error && error.message ? error.message : String(error)) +
          " 表示した結果テキストを全選択して手動でコピーしてください。"
      );
    }
  }

  function downloadResultJson() {
    var json;
    try {
      json = JSON.stringify(createResultPayload(), null, 2);
      var blob = new Blob([json], { type: "application/json;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download =
        "meter_reading_result_" + sheet.business_date.replace(/-/g, "") + ".json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 1000);
      setResultFeedback(
        "結果 JSON のダウンロードを要求しました。保存先でファイルが作成されたことを確認してください。"
      );
    } catch (error) {
      if (json) {
        showResultText(json);
      }
      setResultFeedback(
        "結果 JSON をダウンロードできません: " +
          (error && error.message ? error.message : String(error)) +
          " 表示した JSON を全選択して手動で退避してください。"
      );
    }
  }

  function validateDraft(value) {
    var draftKeys = [
      "kind",
      "v",
      "sheet_id",
      "business_date",
      "generated_at",
      "filter",
      "slots",
    ];
    var slotKeys = [
      "planned_reading_id",
      "measurement_definition_id",
      "meter_id",
      "initial_last_reading",
      "previous_source",
      "initial_previous_scheduled_at",
      "initial_previous_planned_reading_id",
      "result_status",
      "raw",
      "pf_percent",
      "pf_phase",
      "note",
      "confirmed",
      "bad_input",
      "recorded_at",
      "client_updated_at",
    ];
    if (!hasExactKeys(value, draftKeys)) {
      throw new Error("JSON の項目構成が不正です。");
    }
    if (
      value.kind !== DRAFT_KIND ||
      value.v !== 1 ||
      value.sheet_id !== sheet.sheet_id ||
      value.business_date !== sheet.business_date ||
      value.generated_at !== sheet.generated_at
    ) {
      throw new Error("別の検針シート、業務日、出力日時、または未対応の版数です。");
    }
    if (
      !hasExactKeys(value.filter, ["reading_sessions", "cycle_groups"]) ||
      !sameJsonValue(value.filter, sheet.filter)
    ) {
      throw new Error("検針時間または周期の条件が一致しません。");
    }
    if (!Array.isArray(value.slots) || value.slots.length !== slots.length) {
      throw new Error("検針枠の構成が一致しません。");
    }

    return value.slots.map(function (entry, index) {
      var source = slots[index];
      if (!hasExactKeys(entry, slotKeys)) {
        throw new Error("検針枠 " + (index + 1) + " の項目構成が不正です。");
      }
      if (
        entry.planned_reading_id !== source.id ||
        entry.measurement_definition_id !== source.base.measurement_definition_id ||
        entry.meter_id !== source.base.meter_id ||
        entry.initial_last_reading !== source.base.initial_last_reading ||
        entry.previous_source !== source.base.previous_source ||
        entry.initial_previous_scheduled_at !==
          source.base.initial_previous_scheduled_at ||
        entry.initial_previous_planned_reading_id !==
          source.base.initial_previous_planned_reading_id
      ) {
        throw new Error("検針枠または出力時の前回値情報が一致しません。");
      }
      if (
        !Object.prototype.hasOwnProperty.call(STATUS_LABELS, entry.result_status) ||
        typeof entry.raw !== "string" ||
        typeof entry.pf_percent !== "string" ||
        typeof entry.pf_phase !== "string" ||
        typeof entry.note !== "string" ||
        typeof entry.confirmed !== "boolean" ||
        typeof entry.bad_input !== "boolean" ||
        !validIsoTimestamp(entry.recorded_at) ||
        !validIsoTimestamp(entry.client_updated_at)
      ) {
        throw new Error("検針枠 " + (index + 1) + " の入力形式が不正です。");
      }
      var restored = {
        result_status: entry.result_status,
        raw: entry.raw,
        pf_percent: entry.pf_percent,
        pf_phase: entry.pf_phase,
        note: entry.note,
        confirmed: entry.confirmed,
        bad_input: entry.bad_input,
        recorded_at: entry.recorded_at,
        client_updated_at: entry.client_updated_at,
      };
      if (restored.confirmed) {
        var originalState = source.state;
        source.state = restored;
        var validation;
        try {
          validation = validateSlot(source);
        } finally {
          source.state = originalState;
        }
        if (!validation.complete || restored.recorded_at === null) {
          throw new Error("入力条件を満たさない検針枠は確認済みにできません。");
        }
      }
      return restored;
    });
  }

  function validateStorageRecord(record) {
    if (
      !hasExactKeys(record, ["sheet_id", "storage_revision", "draft"]) ||
      record.sheet_id !== sheet.sheet_id ||
      !Number.isSafeInteger(record.storage_revision) ||
      record.storage_revision < 1
    ) {
      return null;
    }
    try {
      return {
        revision: record.storage_revision,
        states: validateDraft(record.draft),
      };
    } catch (error) {
      return null;
    }
  }

  function storageError(code, message, record) {
    var error = new Error(message);
    error.code = code;
    error.record = record || null;
    return error;
  }

  function openDraftDatabase() {
    if (storageDatabase) {
      return Promise.resolve(storageDatabase);
    }
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = null;
      var request;
      function fail(error) {
        if (settled) {
          return;
        }
        settled = true;
        if (timer !== null) {
          clearTimeout(timer);
        }
        reject(error);
      }
      try {
        if (!window.indexedDB || typeof window.indexedDB.open !== "function") {
          throw storageError("unavailable", "このブラウザでは IndexedDB を利用できません。");
        }
        request = window.indexedDB.open(
          STORAGE_DATABASE_NAME,
          STORAGE_DATABASE_VERSION
        );
        request.onupgradeneeded = function () {
          var database = request.result;
          if (!database.objectStoreNames.contains("drafts")) {
            database.createObjectStore("drafts", { keyPath: "sheet_id" });
          }
        };
        request.onblocked = function () {
          fail(
            storageError(
              "blocked",
              "別のページが保存先を使用中です。ほかの検針シートを閉じて再試行してください。"
            )
          );
        };
        request.onerror = function () {
          fail(
            storageError(
              "open",
              request.error ? request.error.message : "保存先を開けませんでした。"
            )
          );
        };
        request.onsuccess = function () {
          var database = request.result;
          if (settled) {
            database.close();
            return;
          }
          settled = true;
          if (timer !== null) {
            clearTimeout(timer);
          }
          storageDatabase = database;
          database.onversionchange = function () {
            database.close();
            if (storageDatabase === database) {
              storageDatabase = null;
            }
            storageWriteAllowed = false;
            showStorageFailure(
              storageError(
                "versionchange",
                "保存先の版数が変更されました。ほかの検針シートを閉じて再試行してください。"
              )
            );
          };
          database.addEventListener("close", function () {
            if (storageDatabase === database) {
              storageDatabase = null;
              storageWriteAllowed = false;
              showStorageFailure(
                storageError(
                  "closed",
                  "保存先との接続が終了しました。入力は画面に保持されています。"
                )
              );
            }
          });
          resolve(database);
        };
        timer = setTimeout(function () {
          fail(storageError("timeout", "保存先への接続がタイムアウトしました。"));
        }, STORAGE_TIMEOUT_MS);
      } catch (error) {
        fail(
          error && error.code
            ? error
            : storageError("open", String(error))
        );
      }
    });
  }

  function readDraftRecord() {
    return openDraftDatabase().then(function (database) {
      return new Promise(function (resolve, reject) {
        var transaction;
        var timer = null;
        var settled = false;
        var record;
        var failure = null;
        function fail(error) {
          if (settled) {
            return;
          }
          settled = true;
          if (timer !== null) {
            clearTimeout(timer);
          }
          reject(error);
        }
        try {
          transaction = database.transaction("drafts", "readonly");
          var request = transaction.objectStore("drafts").get(sheet.sheet_id);
          request.onsuccess = function () {
            record = request.result;
          };
          request.onerror = function () {
            failure = storageError(
              "read",
              request.error ? request.error.message : "途中経過を読み込めませんでした。"
            );
          };
          transaction.onerror = function () {
            failure =
              failure ||
              storageError(
                "read",
                transaction.error
                  ? transaction.error.message
                  : "読込トランザクションに失敗しました。"
              );
          };
          transaction.onabort = function () {
            fail(
              failure ||
                storageError("read", "途中経過の読込処理が中断されました。")
            );
          };
          transaction.oncomplete = function () {
            if (settled) {
              return;
            }
            settled = true;
            if (timer !== null) {
              clearTimeout(timer);
            }
            resolve(record);
          };
          timer = setTimeout(function () {
            var error = storageError("timeout", "途中経過の読込がタイムアウトしました。");
            fail(error);
            try {
              transaction.abort();
            } catch (abortError) {
              setStorageMessage(error.message + " " + String(abortError));
            }
          }, STORAGE_TIMEOUT_MS);
        } catch (error) {
          fail(storageError("read", String(error)));
        }
      });
    });
  }

  function writeDraftRecord(draft, expectedRevision, forceReplace) {
    return openDraftDatabase().then(function (database) {
      return new Promise(function (resolve, reject) {
        var transaction;
        var timer = null;
        var settled = false;
        var nextRevision = null;
        var failure = null;
        function fail(error) {
          if (settled) {
            return;
          }
          settled = true;
          if (timer !== null) {
            clearTimeout(timer);
          }
          reject(error);
        }
        try {
          transaction = database.transaction("drafts", "readwrite");
          var store = transaction.objectStore("drafts");
          var request = store.get(sheet.sheet_id);
          request.onerror = function () {
            failure = storageError(
              "write",
              request.error ? request.error.message : "保存先を確認できませんでした。"
            );
          };
          request.onsuccess = function () {
            var currentRecord = request.result;
            var validated = currentRecord === undefined
              ? null
              : validateStorageRecord(currentRecord);
            if (currentRecord !== undefined && validated === null && !forceReplace) {
              failure = storageError(
                "corrupt",
                "保存先のデータが壊れているため、自動上書きを停止しました。"
              );
            } else if (
              !forceReplace &&
              (validated ? validated.revision : 0) !== expectedRevision
            ) {
              failure = storageError(
                "conflict",
                "別のページで保存内容が更新されています。自動上書きしません。",
                currentRecord
              );
            } else {
              var currentRevision = validated
                ? validated.revision
                : currentRecord &&
                    Number.isSafeInteger(currentRecord.storage_revision) &&
                    currentRecord.storage_revision > 0
                  ? currentRecord.storage_revision
                  : 0;
              if (currentRevision >= Number.MAX_SAFE_INTEGER) {
                failure = storageError(
                  "write",
                  "保存リビジョンの上限に達したため保存できません。"
                );
              } else {
                nextRevision = currentRevision + 1;
                try {
                  store.put({
                    sheet_id: sheet.sheet_id,
                    storage_revision: nextRevision,
                    draft: draft,
                  });
                } catch (error) {
                  failure = storageError("write", String(error));
                }
              }
            }
            if (failure) {
              try {
                transaction.abort();
              } catch (abortError) {
                fail(
                  storageError(
                    failure.code,
                    failure.message + " " + String(abortError),
                    failure.record
                  )
                );
              }
            }
          };
          transaction.onerror = function () {
            failure =
              failure ||
              storageError(
                "write",
                transaction.error
                  ? transaction.error.message
                  : "保存トランザクションに失敗しました。"
              );
          };
          transaction.onabort = function () {
            fail(
              failure ||
                storageError("write", "途中経過の保存処理が中断されました。")
            );
          };
          transaction.oncomplete = function () {
            if (settled) {
              return;
            }
            settled = true;
            if (timer !== null) {
              clearTimeout(timer);
            }
            resolve(nextRevision);
          };
          timer = setTimeout(function () {
            var error = storageError("timeout", "途中経過の保存がタイムアウトしました。");
            fail(error);
            try {
              transaction.abort();
            } catch (abortError) {
              setStorageMessage(error.message + " " + String(abortError));
            }
          }, STORAGE_TIMEOUT_MS);
        } catch (error) {
          fail(storageError("write", String(error)));
        }
      });
    });
  }

  function showStorageFailure(error) {
    storageWriteAllowed = false;
    setStorageStatus("error", "保存失敗");
    setStorageMessage(
      "途中経過を安全に読み書きできません: " +
        error.message +
        " 入力を続ける場合は JSON で退避し、自動上書きしないでください。"
    );
    storageRetryButton.hidden = false;
    storageManualButton.hidden = false;
    storageReplaceButton.hidden = storageDatabase === null;
    storageConflictNode.hidden = true;
    setDraftActionsEnabled(true);
  }

  function showStorageConflict(record) {
    storageWriteAllowed = false;
    draftConflictCandidate = record;
    storageConflictNode.hidden = false;
    storageUseStoredButton.hidden = record === null;
    storageConflictMessageNode.textContent =
      record === null
        ? "別ページまたは壊れた保存データを検出しました。読み込みを確認できないデータは自動で置き換えません。現在の入力を退避してから選択してください。"
        : "別のページに新しい入力があります。保存済みの入力を読み込むか、現在の入力で置き換えるか選んでください。自動マージはしません。";
    storageRetryButton.hidden = false;
    storageManualButton.hidden = false;
    storageReplaceButton.hidden = record !== null || storageDatabase === null;
    storageReplaceConfirmationNode.hidden = true;
    setStorageStatus("error", "保存失敗: 別ページとの競合");
    setStorageMessage(
      "競合が解決するまで自動保存を停止しています。入力内容はこの画面に保持されています。"
    );
    setMainInputEnabled(true);
    setDraftActionsEnabled(true);
  }

  function applyDraftStates(states, revision) {
    clearResultOutput();
    slots.forEach(function (slot, index) {
      slot.state = states[index];
    });
    storageRevision = revision;
    storageInitialized = true;
    storageWriteAllowed = true;
    savedGeneration = editGeneration;
    pendingSave = null;
    draftConflictCandidate = null;
    storageConflictNode.hidden = true;
    storageReplaceConfirmationNode.hidden = true;
    storageRetryButton.hidden = true;
    storageManualButton.hidden = true;
    storageReplaceButton.hidden = true;
    setStorageMessage("");
    setMainInputEnabled(true);
    setDraftActionsEnabled(true);
    refresh();
    setStorageStatus("saved", "保存済み");
  }

  function displayStorageConflict(record) {
    var validated = record === null ? null : validateStorageRecord(record);
    showStorageConflict(
      validated
        ? { revision: validated.revision, states: validated.states }
        : null
    );
  }

  function initializeDraftStorage() {
    setStorageStatus("loading", "読込中");
    readDraftRecord()
      .then(function (record) {
        if (record === undefined) {
          storageRevision = 0;
          storageInitialized = true;
          storageWriteAllowed = true;
          savedGeneration = editGeneration;
          storageRetryButton.hidden = true;
          storageManualButton.hidden = true;
          storageReplaceButton.hidden = true;
          setStorageMessage("");
          setMainInputEnabled(true);
          setDraftActionsEnabled(true);
          setStorageStatus("ready", "保存先を確認しました。入力待ちです");
          return;
        }
        var validated = validateStorageRecord(record);
        if (validated === null) {
          throw storageError(
            "corrupt",
            "保存データの形式または検針シート情報が一致しません。"
          );
        }
        applyDraftStates(validated.states, validated.revision);
      })
      .catch(function (error) {
        showStorageFailure(
          error && error.message
            ? error
            : storageError("read", String(error))
        );
      });
  }

  function retryDraftStorage() {
    setStorageStatus("loading", "読込中");
    setStorageMessage("");
    setMainInputEnabled(false);
    storageRetryButton.disabled = true;
    readDraftRecord()
      .then(function (record) {
        storageRetryButton.disabled = false;
        if (record === undefined) {
          storageRevision = 0;
          storageInitialized = true;
          storageWriteAllowed = true;
          storageRetryButton.hidden = true;
          storageManualButton.hidden = true;
          storageReplaceButton.hidden = true;
          storageConflictNode.hidden = true;
          setMainInputEnabled(true);
          setDraftActionsEnabled(true);
          if (editGeneration > savedGeneration) {
            pendingSave = {
              generation: editGeneration,
              draft: createDraft(),
              forceReplace: false,
            };
            setStorageStatus("saving", "保存中");
            void flushDraftSaveQueue();
          } else {
            savedGeneration = editGeneration;
            setStorageStatus("ready", "保存先を確認しました。入力待ちです");
          }
          return;
        }
        var validated = validateStorageRecord(record);
        if (validated === null) {
          throw storageError(
            "corrupt",
            "保存データの形式または検針シート情報が一致しません。"
          );
        }
        if (editGeneration > savedGeneration && validated.revision !== storageRevision) {
          storageInitialized = true;
          displayStorageConflict(record);
          return;
        }
        if (editGeneration > savedGeneration) {
          storageInitialized = true;
          storageWriteAllowed = true;
          storageRetryButton.hidden = true;
          storageManualButton.hidden = true;
          storageReplaceButton.hidden = true;
          setStorageMessage("");
          setMainInputEnabled(true);
          setDraftActionsEnabled(true);
          pendingSave = {
            generation: editGeneration,
            draft: createDraft(),
            forceReplace: false,
          };
          setStorageStatus("saving", "保存中");
          void flushDraftSaveQueue();
          return;
        }
        applyDraftStates(validated.states, validated.revision);
      })
      .catch(function (error) {
        storageRetryButton.disabled = false;
        showStorageFailure(
          error && error.message
            ? error
            : storageError("read", String(error))
        );
      });
  }

  function requestDraftReplacement() {
    if (storageDatabase === null) {
      setStorageMessage(
        "保存先に接続できません。保存せずに続行し、JSON を手動で退避してください。"
      );
      return;
    }
    storageReplaceConfirmationNode.hidden = false;
  }

  function confirmDraftReplacement() {
    if (storageDatabase === null) {
      showStorageFailure(
        storageError("closed", "保存先に接続できないため置き換えできません。")
      );
      return;
    }
    storageInitialized = true;
    storageWriteAllowed = true;
    storageRetryButton.hidden = true;
    storageManualButton.hidden = true;
    storageReplaceButton.hidden = true;
    storageConflictNode.hidden = true;
    storageReplaceConfirmationNode.hidden = true;
    draftConflictCandidate = null;
    setMainInputEnabled(true);
    setDraftActionsEnabled(true);
    setStorageMessage("");
    scheduleDraftSave(true);
  }

  function scheduleDraftSave(forceReplace) {
    if (!storageInitialized) {
      return;
    }
    editGeneration += 1;
    if (!storageWriteAllowed) {
      if (storageStatusNode.dataset.status !== "error") {
        setStorageStatus("error", "保存失敗");
      }
      return;
    }
    pendingSave = {
      generation: editGeneration,
      draft: createDraft(),
      forceReplace: forceReplace,
    };
    setStorageStatus("saving", "保存中");
    void flushDraftSaveQueue();
  }

  async function flushDraftSaveQueue() {
    if (saveQueueActive || !storageWriteAllowed) {
      return;
    }
    saveQueueActive = true;
    try {
      while (pendingSave && storageWriteAllowed) {
        var job = pendingSave;
        pendingSave = null;
        try {
          storageRevision = await writeDraftRecord(
            job.draft,
            storageRevision,
            job.forceReplace
          );
          savedGeneration = Math.max(savedGeneration, job.generation);
          if (job.generation === editGeneration && pendingSave === null) {
            storageRetryButton.hidden = true;
            storageManualButton.hidden = true;
            storageReplaceButton.hidden = true;
            setStorageMessage("");
            setStorageStatus("saved", "保存済み");
          } else {
            setStorageStatus("saving", "保存中");
          }
        } catch (error) {
          if (error && (error.code === "conflict" || error.code === "corrupt")) {
            pendingSave = null;
            displayStorageConflict(error.record || null);
            return;
          }
          if (pendingSave !== null && pendingSave.generation > job.generation) {
            setStorageStatus("saving", "保存中");
            continue;
          }
          pendingSave = null;
          storageRetryButton.hidden = false;
          storageManualButton.hidden = false;
          storageReplaceButton.hidden = storageDatabase === null;
          setStorageStatus("error", "保存失敗");
          setStorageMessage(
            "途中経過を保存できませんでした。入力は画面に保持されています。JSON で退避するか、保存先を再確認してください。" +
              (error && error.message ? " " + error.message : "")
          );
          return;
        }
      }
    } finally {
      saveQueueActive = false;
      if (pendingSave && storageWriteAllowed) {
        void flushDraftSaveQueue();
      }
    }
  }

  function clearDraftImportCandidate() {
    draftImportCandidate = null;
    draftPreviewNode.hidden = true;
    draftPreviewSummaryNode.textContent = "";
  }

  function inspectDraftText(text, generation) {
    var readGeneration =
      generation === undefined ? ++draftReadGeneration : generation;
    clearDraftImportCandidate();
    try {
      if (typeof text !== "string" || text.trim().length === 0) {
        throw new Error("JSON が空です。");
      }
      var parsed;
      try {
        parsed = JSON.parse(text);
      } catch (parseError) {
        if (text.indexOf("MR1:") !== -1) {
          throw new Error("MR1 は結果コードです。途中経過 JSON としては読み込めません。");
        }
        throw parseError;
      }
      if (parsed && parsed.kind === "mrm-meter-reading-result") {
        throw new Error("結果 JSON は途中経過として読み込めません。");
      }
      var states = validateDraft(parsed);
      if (readGeneration !== draftReadGeneration) {
        return;
      }
      draftImportCandidate = states;
      var confirmedCount = states.filter(function (state) {
        return state.confirmed;
      }).length;
      draftPreviewSummaryNode.textContent =
        sheet.business_date +
        " / " +
        states.length +
        " 枠 / " +
        confirmedCount +
        " 枠を確認済みとして復元します。承認するまで画面と保存先は変更しません。";
      draftPreviewNode.hidden = false;
      setDraftFeedback("途中経過 JSON を検証しました。全体置換を承認してください。");
    } catch (error) {
      if (readGeneration !== draftReadGeneration) {
        return;
      }
      setDraftFeedback(
        "途中経過 JSON を読み込めません: " +
          (error && error.message ? error.message : String(error))
      );
    }
  }

  function readDraftFile(file) {
    var generation = ++draftReadGeneration;
    clearDraftImportCandidate();
    if (!file) {
      setDraftFeedback("読み込むファイルを選択してください。");
      return;
    }
    try {
      var reader = new FileReader();
      reader.onload = function () {
        if (generation !== draftReadGeneration) {
          return;
        }
        if (typeof reader.result !== "string") {
          setDraftFeedback("ファイルの内容をテキストとして読み込めませんでした。");
          return;
        }
        draftImportText.value = reader.result;
        inspectDraftText(reader.result, generation);
      };
      reader.onerror = function () {
        if (generation === draftReadGeneration) {
          setDraftFeedback(
            "ファイルを読み込めません: " +
              (reader.error ? reader.error.message : "読み込みエラー")
          );
        }
      };
      reader.onabort = function () {
        if (generation === draftReadGeneration) {
          setDraftFeedback(
            "ファイルの読み込みがキャンセルされました。画面と保存先は変更していません。"
          );
        }
      };
      reader.readAsText(file, "UTF-8");
    } catch (error) {
      setDraftFeedback(
        "ファイルを読み込めません: " +
          (error && error.message ? error.message : String(error))
      );
    }
  }

  function confirmDraftImport() {
    if (!draftImportCandidate) {
      return;
    }
    clearResultOutput();
    slots.forEach(function (slot, index) {
      slot.state = draftImportCandidate[index];
    });
    draftImportCandidate = null;
    draftPreviewNode.hidden = true;
    draftImportText.value = "";
    editGeneration += 1;
    refresh();
    setDraftFeedback("途中経過を全体置換しました。");
    if (storageInitialized && storageWriteAllowed) {
      pendingSave = {
        generation: editGeneration,
        draft: createDraft(),
        forceReplace: false,
      };
      setStorageStatus("saving", "保存中");
      void flushDraftSaveQueue();
    }
  }

  function showDraftJson() {
    draftExportText.value = JSON.stringify(createDraft(), null, 2);
    draftExportText.hidden = false;
    draftExportText.focus();
    draftExportText.select();
    setDraftFeedback(
      "JSON を表示しました。保存できない場合は全選択して手動でコピーしてください。"
    );
  }

  function downloadDraft() {
    try {
      var blob = new Blob([JSON.stringify(createDraft(), null, 2)], {
        type: "application/json;charset=utf-8",
      });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download =
        "meter_reading_draft_" + sheet.business_date.replace(/-/g, "") + ".json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 1000);
      setDraftFeedback(
        "JSON のダウンロードを要求しました。保存先でファイルが作成されたことを確認してください。"
      );
    } catch (error) {
      setDraftFeedback(
        "JSON をダウンロードできません: " +
          (error && error.message ? error.message : String(error)) +
          "「JSON を表示」から手動でコピーしてください。"
      );
    }
  }

  async function copyDraftJson() {
    showDraftJson();
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("この環境ではクリップボードを利用できません。");
      }
      await navigator.clipboard.writeText(draftExportText.value);
      setDraftFeedback("途中経過 JSON をクリップボードにコピーしました。");
    } catch (error) {
      draftExportText.focus();
      draftExportText.select();
      setDraftFeedback(
        "コピーできませんでした: " +
          (error && error.message ? error.message : String(error)) +
          " 表示した JSON を全選択して手動でコピーしてください。"
      );
    }
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

    toolbarNode = element("div", "toolbar");
    var locationItem = element("label", "toolbar-item");
    locationItem.appendChild(element("span", "", "場所"));
    locationSelect = document.createElement("select");
    locationSelect.className = "location-select";
    locationSelect.addEventListener("change", function () {
      view.location = locationSelect.value;
      refresh();
    });
    locationItem.appendChild(locationSelect);
    toolbarNode.appendChild(locationItem);
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
    toolbarNode.appendChild(toggleItem);
    app.appendChild(toolbarNode);
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
    locationsNode = list;
    app.appendChild(locationsNode);
    emptyMessage = element("p", "empty-message", "表示できる検針枠がありません。");
    app.appendChild(emptyMessage);
  }

  buildHeader();
  buildDraftTools();
  buildNavigation();
  buildSections();
  buildResultTools();
  view.tab = initialTab();
  refresh();
  centerTab(view.tab);
  setMainInputEnabled(false);
  initializeDraftStorage();
  window.addEventListener("resize", updateStickyOffset);
})();
