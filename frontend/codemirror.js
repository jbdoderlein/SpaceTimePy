import {Compartment, EditorState, StateEffect, StateField} from "@codemirror/state";
import {
  Decoration,
  EditorView,
  highlightSpecialChars,
  lineNumbers,
} from "@codemirror/view";
import {HighlightStyle, syntaxHighlighting} from "@codemirror/language";
import {pythonLanguage} from "@codemirror/lang-python";
import {tags} from "@lezer/highlight";

const setActiveLine = StateEffect.define();

const activeLineField = StateField.define({
  create() {
    return Decoration.none;
  },
  update(decorations, transaction) {
    for (const effect of transaction.effects) {
      if (!effect.is(setActiveLine)) continue;
      if (effect.value == null) return Decoration.none;
      const line = transaction.state.doc.line(effect.value);
      return Decoration.set([
        Decoration.line({class: "cm-activeTraceLine"}).range(line.from),
      ]);
    }
    return decorations.map(transaction.changes);
  },
  provide: field => EditorView.decorations.from(field),
});

const syntaxColors = HighlightStyle.define([
  {tag: tags.keyword, color: "var(--syntax-keyword)", fontWeight: "650"},
  {
    tag: [tags.name, tags.deleted, tags.character, tags.propertyName, tags.macroName],
    color: "var(--syntax-name)",
  },
  {
    tag: [tags.function(tags.variableName), tags.labelName],
    color: "var(--syntax-function)",
  },
  {
    tag: [tags.color, tags.constant(tags.name), tags.standard(tags.name)],
    color: "var(--syntax-constant)",
  },
  {
    tag: [tags.definition(tags.name), tags.separator],
    color: "var(--syntax-definition)",
  },
  {
    tag: [tags.typeName, tags.className, tags.number, tags.changed, tags.annotation],
    color: "var(--syntax-type)",
  },
  {
    tag: [tags.operator, tags.operatorKeyword, tags.url, tags.escape, tags.regexp],
    color: "var(--syntax-operator)",
  },
  {
    tag: [tags.meta, tags.comment],
    color: "var(--syntax-comment)",
    fontStyle: "italic",
  },
  {
    tag: [tags.string, tags.special(tags.string), tags.inserted],
    color: "var(--syntax-string)",
  },
  {tag: tags.invalid, color: "var(--danger)", textDecoration: "underline"},
]);

const editorStyles = {
  "&": {
    height: "100%",
    color: "var(--code-text)",
    backgroundColor: "var(--code)",
    fontSize: "13px",
  },
  ".cm-scroller": {
    overflow: "auto",
    fontFamily: "ui-monospace,SFMono-Regular,Consolas,monospace",
    lineHeight: "1.55",
  },
  ".cm-content": {
    padding: "12px 0",
    caretColor: "transparent",
  },
  ".cm-line": {
    padding: "0 14px 0 8px",
  },
  ".cm-gutters": {
    color: "var(--code-line-number)",
    backgroundColor: "var(--code)",
    border: "none",
    paddingLeft: "6px",
  },
  ".cm-activeLineGutter": {
    color: "var(--code-highlight-text)",
    backgroundColor: "var(--code-highlight)",
  },
  ".cm-activeTraceLine": {
    color: "var(--code-highlight-text)",
    backgroundColor: "var(--code-highlight)",
  },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "var(--code-selection) !important",
  },
  ".cm-cursor": {
    display: "none",
  },
  "&.cm-focused": {
    outline: "none",
  },
};

const lightTheme = EditorView.theme(editorStyles, {dark: false});
const darkTheme = EditorView.theme(editorStyles, {dark: true});
const darkPreference = window.matchMedia("(prefers-color-scheme: dark)");
const liveViews = new Set();

function selectedTheme() {
  return darkPreference.matches ? darkTheme : lightTheme;
}

function reconfigureThemes() {
  for (const codeView of liveViews) {
    codeView.view.dispatch({
      effects: codeView.theme.reconfigure(selectedTheme()),
    });
  }
}

darkPreference.addEventListener?.("change", reconfigureThemes);

export class ReadOnlyCodeView {
  constructor(parent) {
    this.hasSource = false;
    this.theme = new Compartment();
    this.gutter = new Compartment();
    this.view = new EditorView({
      parent,
      state: EditorState.create({
        doc: "",
        extensions: [
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          highlightSpecialChars(),
          pythonLanguage,
          syntaxHighlighting(syntaxColors),
          activeLineField,
          this.gutter.of(lineNumbers()),
          this.theme.of(selectedTheme()),
        ],
      }),
    });
    liveViews.add(this);
  }

  setSource(source, {firstLine = 1, activeLine = null} = {}) {
    this.hasSource = true;
    const relativeLine =
      activeLine != null && activeLine >= firstLine
        ? activeLine - firstLine + 1
        : null;
    const validActiveLine =
      relativeLine != null && relativeLine <= source.split("\n").length
        ? relativeLine
        : null;
    this.view.dispatch({
      changes: {
        from: 0,
        to: this.view.state.doc.length,
        insert: source,
      },
      effects: [
        this.gutter.reconfigure(
          lineNumbers({
            formatNumber: number => String(firstLine + number - 1),
          }),
        ),
        setActiveLine.of(validActiveLine),
      ],
    });
    if (validActiveLine != null) {
      const line = this.view.state.doc.line(validActiveLine);
      this.view.dispatch({
        effects: EditorView.scrollIntoView(line.from, {y: "center"}),
      });
    }
  }

  clearHighlight() {
    this.view.dispatch({effects: setActiveLine.of(null)});
  }

  destroy() {
    liveViews.delete(this);
    this.view.destroy();
  }
}
