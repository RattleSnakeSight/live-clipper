// Renders setup questions (GET /api/setup/questions). Used by the wizard and by Settings.

import { h, nextId } from './ui.js';

export function condOk(c, a) {
  const v = a[c.id];
  if ('equals' in c) return v === c.equals;
  if ('not_equals' in c) return v !== c.not_equals;
  if ('in' in c) return c.in.includes(v);
  if ('contains_any' in c) return (v || []).some((x) => c.contains_any.includes(x));
  if ('not_empty' in c) return Boolean(v && (!Array.isArray(v) || v.length));
  return true;
}

export const isVisible = (q, answers) => (q.show_if || []).every((c) => condOk(c, answers));

export function effective(questions, answers) {
  const out = {};
  for (const q of questions) out[q.id] = q.id in answers ? answers[q.id] : q.default;
  return out;
}

export function answerLabel(q, v) {
  if (q.type === 'boolean') return v ? 'Yes' : 'No';
  const label = (x) => (q.options.find((o) => o.value === x) || { label: String(x) }).label;
  if (q.type === 'multiselect') return (v || []).map(label).join(', ') || 'None';
  if (q.type === 'select') return label(v);
  return v === '' || v === null || v === undefined ? 'Not set' : String(v);
}

// Client-side checks mirror the server's; the server stays the source of truth.
export function checkValue(q, v) {
  const val = q.validation || {};
  if (q.type === 'number') {
    if (typeof v !== 'number' || Number.isNaN(v)) return 'Enter a number.';
    if (val.integer && !Number.isInteger(v)) return 'Enter a whole number.';
    if ('min' in val && v < val.min) return `Enter a number from ${val.min} to ${val.max}.`;
    if ('max' in val && v > val.max) return `Enter a number from ${val.min} to ${val.max}.`;
  }
  if (q.type === 'multiselect' && val.min_items && (v || []).length < val.min_items) return 'Pick at least one.';
  if (q.type === 'boolean' && val.must_be && v !== true) return 'This needs a yes to continue.';
  if ((q.type === 'url' || q.type === 'path') && val.required && !String(v || '').trim()) return 'This needs an answer.';
  if (q.type === 'url' && v && !/^https?:\/\/[^/\s]+/.test(v)) return 'Enter an address that starts with http:// or https://.';
  return null;
}

// Returns {el, read(), setError(msg), focus()}. `big` is the wizard: an h2 title, and Yes and No as two answers.
// Settings (not big) shows a yes/no answer as a switch and puts short controls beside their label.
export function questionField(q, value, { big = false } = {}) {
  const id = nextId('q');
  const helpId = `${id}-help`;
  const errId = `${id}-err`;
  const titleId = `${id}-title`;
  const Title = (inner) => (big ? h('h2', { id: titleId }, inner) : h('div', { id: titleId, class: 'label' }, inner));
  // q.note: something to know before answering (who posts where, what needs no account). A box in the wizard, a
  // second help line in Settings. q.intent names which of the two reasons to connect an account the question starts.
  const noteId = `${id}-note`;
  const noteEl = q.note ? h('p', { class: big ? 'note info' : 'help', id: noteId }, q.note) : null;
  const eyebrow = big && q.intent ? h('p', { class: 'eyebrow', id: `${id}-intent` }, q.intent) : null;
  const helpEl = [h('p', { class: 'help', id: helpId }, q.help), noteEl];
  const errEl = h('p', { class: 'error', id: errId, role: 'alert' });
  const described = `${helpId} ${errId}`;
  // the intent and the note are read once, with the group, not again on every option
  const groupDescribed = `${eyebrow ? `${id}-intent ` : ''}${helpId}${q.note ? ` ${noteId}` : ''} ${errId}`;
  let read;
  let control;
  let first;

  const group = (type, opts, chosen) => {
    const inputs = opts.map((o) => h('input', {
      type, name: id, value: o.value, checked: chosen.includes(o.value), help: q.help, 'aria-describedby': described,
    }));
    control = h('div', { role: type === 'radio' ? 'radiogroup' : 'group', 'aria-labelledby': titleId, 'aria-describedby': groupDescribed },
      opts.map((o, i) => h('label', { class: 'choice' }, inputs[i], h('span', {}, o.label))));
    first = inputs.find((i) => i.checked) || inputs[0];
    return inputs;
  };

  if (q.type === 'boolean' && !big) {
    control = h('input', { id, type: 'checkbox', role: 'switch', class: 'switch', checked: Boolean(value), help: q.help,
      'aria-describedby': described });
    first = control;
    read = () => control.checked;
  } else if (q.type === 'boolean') {
    const inputs = group('radio', [{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }], [value ? 'yes' : 'no']);
    read = () => inputs[0].checked;
  } else if (q.type === 'multiselect') {
    const inputs = group('checkbox', q.options, value || []);
    read = () => inputs.filter((i) => i.checked).map((i) => i.value);
  } else if (q.type === 'select' && q.options.length <= 5) {
    const inputs = group('radio', q.options, [value]);
    read = () => (inputs.find((i) => i.checked) || {}).value;
  } else if (q.type === 'select') {
    control = h('select', { id, help: q.help, 'aria-describedby': described },
      q.options.map((o) => h('option', { value: o.value }, o.label)));
    control.value = value;
    first = control;
    read = () => control.value;
  } else {
    const type = q.type === 'number' ? 'number' : q.type === 'url' ? 'url' : 'text';
    const v = q.validation || {};
    control = h('input', {
      id, type, help: q.help, 'aria-describedby': described, min: v.min, max: v.max,
      step: v.integer ? 1 : 'any', inputmode: type === 'number' ? 'numeric' : null,
      autocomplete: 'off', spellcheck: 'false', required: v.required,
    });
    control.value = value ?? '';
    first = control;
    read = () => (q.type === 'number' ? (control.value === '' ? NaN : Number(control.value)) : control.value.trim());
  }
  const labelled = control.tagName === 'DIV' ? Title(q.title) : Title(h('label', { for: id }, q.title));
  const isSwitch = control.classList.contains('switch');
  const short = !big && (control.tagName === 'SELECT' || control.type === 'number');
  const el = isSwitch
    ? h('div', { class: 'field', dataset: { qid: q.id } },
      h('div', { class: 'switch-row' }, h('div', { class: 'text' }, labelled, helpEl), control), errEl)
    : h('div', { class: `${big ? 'question' : 'field'}${short ? ' short' : ''}`, dataset: { qid: q.id } }, eyebrow, labelled, helpEl, control, errEl);
  return {
    el,
    read,
    focus: () => first && first.focus(),
    setError(msg) {
      errEl.textContent = msg || '';
      for (const c of el.querySelectorAll('input, select')) {
        if (msg) c.setAttribute('aria-invalid', 'true'); else c.removeAttribute('aria-invalid');
      }
    },
  };
}
