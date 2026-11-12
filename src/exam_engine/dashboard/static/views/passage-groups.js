function itemOrder(item, fallbackIndex) {
  const passageOrder = Number(item?.passage_order);
  if (Number.isFinite(passageOrder)) return passageOrder;
  const sourceRow = Number(item?.source_row);
  if (Number.isFinite(sourceRow)) return sourceRow;
  return fallbackIndex;
}

function passageIdOf(item) {
  return String(item?.passage?.passage_id || item?.passage_id || "").trim();
}

/**
 * Build contiguous rendering groups while preserving the API's independent
 * question order.  A passage's questions are ordered by the stored
 * passage_order (then source row) so a page cannot scramble the source sequence.
 */
export function groupQuestionsByPassage(questions) {
  const groups = [];
  const byPassage = new Map();
  for (const [index, item] of (Array.isArray(questions) ? questions : []).entries()) {
    if (!item) continue;
    const passageId = passageIdOf(item);
    if (!passageId) {
      groups.push({ passage: null, questions: [item], firstIndex: index });
      continue;
    }
    let group = byPassage.get(passageId);
    if (!group) {
      group = {
        passage: item.passage || { passage_id: passageId },
        questions: [],
        firstIndex: index,
      };
      byPassage.set(passageId, group);
      groups.push(group);
    } else if (!group.passage?.text && item.passage?.text) {
      group.passage = item.passage;
    }
    group.questions.push({ ...item, _groupIndex: index });
  }
  for (const group of groups) {
    if (!group.passage) continue;
    group.questions = group.questions
      .map((item, index) => ({ item, index }))
      .sort(
        (left, right) =>
          itemOrder(left.item, left.index) - itemOrder(right.item, right.index) ||
          left.index - right.index,
      )
      .map(({ item }) => {
        const { _groupIndex, ...cleanItem } = item;
        return cleanItem;
      });
  }
  return groups;
}

export function passageLabel(passage) {
  if (!passage) return "Passage";
  return passage.source_reference || passage.passage_id || "Passage";
}
