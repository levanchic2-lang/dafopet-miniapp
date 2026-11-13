Component({
  properties: {
    seed: { type: String, value: "" },
    seedKey: { type: Number, value: 0 },
    placeholder: { type: String, value: "" },
  },
  data: {
    text: "",
  },
  lifetimes: {
    attached() {
      this._seenKey = this.properties.seedKey;
      this.setData({ text: this.properties.seed || "" });
    },
  },
  observers: {
    seedKey(key) {
      if (key === this._seenKey) return;
      this._seenKey = key;
      this.setData({ text: this.properties.seed || "" });
    },
  },
  methods: {
    onInput(e) {
      const value = e && e.detail ? (e.detail.value || "") : "";
      this.setData({ text: value });
      this.triggerEvent("input", { value: value });
    },
    onBlur(e) {
      const value = e && e.detail ? (e.detail.value || "") : "";
      this.setData({ text: value });
      this.triggerEvent("blur", { value: value });
    },
  },
});
