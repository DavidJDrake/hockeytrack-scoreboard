// A document with just enough in it to build controls and press them. Not a
// DOM: if something is built and never appended it will not be found here,
// which is the point. Shared by the tests of the modules that build DOM.
export function fakeDoc() {
  return {
    createElement(tag) {
      return {
        tagName: tag, children: [], listeners: {}, dataset: {}, attrs: {}, text: "", disabled: false, hidden: false, value: "",
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
        setAttribute(name, value) { this.attrs[name] = String(value); },
        append(...kids) {
          for (const kid of kids) {
            this.children.push(kid);
            if (typeof kid === "string") this.text += kid;
          }
        },
        replaceChildren(...kids) {
          this.children = [];
          this.text = "";
          this.append(...kids);
        },
        contains(other) { return every(this).includes(other); },
        focus() { this.focused = true; },
        scrollIntoView() { this.scrolledTo = true; },
        fire(type, event = {}) { for (const fn of this.listeners[type] ?? []) fn({ preventDefault() {}, ...event }); },
      };
    },
  };
}

export function every(node, out = []) {
  out.push(node);
  for (const kid of node.children ?? []) if (typeof kid === "object") every(kid, out);
  return out;
}
