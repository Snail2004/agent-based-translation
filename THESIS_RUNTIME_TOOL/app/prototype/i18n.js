(function () {
  var STORAGE_KEY = "thesis.workspace.locale.v1";
  var LEGACY_CONSOLE_KEY = "thesis.agentconsole.locale.v1";
  var CHANGE_EVENT = "thesis:workspace-locale-change";
  var LOCALES = new Set(["vi", "en"]);

  function normalize(value) {
    var locale = String(value || "").trim().toLowerCase();
    return LOCALES.has(locale) ? locale : "vi";
  }

  function read() {
    try {
      return normalize(localStorage.getItem(STORAGE_KEY) || localStorage.getItem(LEGACY_CONSOLE_KEY));
    } catch (_error) {
      return "vi";
    }
  }

  var current = read();

  function applyDocumentLanguage(locale) {
    if (typeof document !== "undefined" && document.documentElement) {
      document.documentElement.lang = locale;
    }
  }

  function write(locale) {
    var next = normalize(locale);
    current = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
      localStorage.setItem(LEGACY_CONSOLE_KEY, next);
    } catch (_error) {
      // Locale persistence is optional; the active tab must keep working.
    }
    applyDocumentLanguage(next);
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { locale: next } }));
    return next;
  }

  function interpolate(value, params) {
    var table = params && typeof params === "object" ? params : {};
    return String(value == null ? "" : value).replace(/\{([a-zA-Z0-9_]+)\}/g, function (_match, key) {
      return Object.prototype.hasOwnProperty.call(table, key) ? String(table[key]) : "{" + key + "}";
    });
  }

  function text(vi, en, params) {
    return interpolate(current === "en" ? (en == null ? vi : en) : vi, params);
  }

  function subscribe(listener) {
    function handle(event) {
      listener(normalize(event && event.detail && event.detail.locale));
    }
    window.addEventListener(CHANGE_EVENT, handle);
    return function () { window.removeEventListener(CHANGE_EVENT, handle); };
  }

  window.addEventListener("storage", function (event) {
    if (event.key !== STORAGE_KEY && event.key !== LEGACY_CONSOLE_KEY) return;
    var next = read();
    if (next === current) return;
    current = next;
    applyDocumentLanguage(next);
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { locale: next } }));
  });

  applyDocumentLanguage(current);

  window.ThesisI18n = Object.freeze({
    storageKey: STORAGE_KEY,
    locales: Object.freeze(["vi", "en"]),
    eventName: CHANGE_EVENT,
    getLocale: function () { return current; },
    setLocale: write,
    text: text,
    subscribe: subscribe,
  });

  window.uiText = text;
  window.useThesisLocale = function useThesisLocale() {
    var state = React.useState(function () { return current; });
    var locale = state[0];
    var setLocaleState = state[1];
    React.useEffect(function () { return subscribe(setLocaleState); }, []);
    var setLocale = React.useCallback(function (next) { return write(next); }, []);
    return [locale, setLocale];
  };

  window.ThesisLocaleSwitch = function ThesisLocaleSwitch(props) {
    var locale = normalize(props && props.locale ? props.locale : current);
    var onChange = props && props.onChange ? props.onChange : write;
    var compact = !!(props && props.compact);
    return React.createElement(
      "span",
      {
        className: "thesis-locale-switch" + (compact ? " is-compact" : ""),
        role: "group",
        "aria-label": text("Ngôn ngữ giao diện", "Interface language"),
      },
      ["vi", "en"].map(function (item) {
        return React.createElement(
          "button",
          {
            key: item,
            type: "button",
            className: "thesis-locale-option" + (locale === item ? " active" : ""),
            "aria-pressed": locale === item,
            title: text("Đổi ngôn ngữ giao diện", "Change interface language") + ": " + item.toUpperCase(),
            onClick: function () { onChange(item); },
          },
          item.toUpperCase()
        );
      })
    );
  };
})();
