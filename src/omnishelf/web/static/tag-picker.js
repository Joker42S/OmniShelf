/* 标签筛选选择器：左边多选标签，中间「包含/排除」按钮把它加到右边的文本框。
   右边两个框是真正的表单字段（tags_include / tags_exclude，逗号分隔），
   没有 JS 时也能直接手工输入。
   纯函数挂在 OmniShelfTagPicker 上，方便 node 里单测（见 tests/test_web_tag_picker.py）。 */
(function (root) {
  "use strict";

  /** "科幻, 经典，动作" -> ["科幻", "经典", "动作"]（中英文逗号都认，去空去重） */
  function splitTags(value) {
    var seen = [];
    String(value === null || value === undefined ? "" : value)
      .split(/[,，]/)
      .forEach(function (part) {
        var name = part.trim();
        if (name && seen.indexOf(name) === -1) seen.push(name);
      });
    return seen;
  }

  /** 把 names 合并进文本框已有的标签里，返回新的框内容（去重、逗号 + 空格连接） */
  function mergeTagList(value, names) {
    var merged = splitTags(value);
    (names || []).forEach(function (name) {
      var clean = String(name).trim();
      if (clean && merged.indexOf(clean) === -1) merged.push(clean);
    });
    return merged.join(", ");
  }

  function initPicker(picker) {
    var pool = picker.querySelector("[data-tag-pool]");
    var targets = {
      include: picker.querySelector("[data-tag-target='include']"),
      exclude: picker.querySelector("[data-tag-target='exclude']")
    };
    if (!pool || !targets.include || !targets.exclude) return;

    function addTo(input, names) {
      if (!names.length) return;
      input.value = mergeTagList(input.value, names);
      input.classList.add("flash");
      root.setTimeout(function () { input.classList.remove("flash"); }, 400);
    }

    picker.querySelectorAll("[data-tag-move]").forEach(function (button) {
      button.addEventListener("click", function () {
        var selected = Array.prototype.slice
          .call(pool.selectedOptions)
          .map(function (option) { return option.value; });
        if (!selected.length) {
          pool.focus();
          return;
        }
        addTo(targets[button.getAttribute("data-tag-move")], selected);
        Array.prototype.forEach.call(pool.options, function (option) {
          option.selected = false; // 加完取消选中，避免再点一次重复添加
        });
      });
    });

    // 双击左边标签 = 直接加入「包含」，省一次点按钮
    pool.addEventListener("dblclick", function (event) {
      if (!event.target || event.target.tagName !== "OPTION") return;
      addTo(targets.include, [event.target.value]);
      event.target.selected = false;
    });
  }

  var api = { splitTags: splitTags, mergeTagList: mergeTagList, initPicker: initPicker };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.OmniShelfTagPicker = api;

  if (typeof document !== "undefined") {
    document.querySelectorAll("[data-tag-picker]").forEach(initPicker);
  }
})(typeof window !== "undefined" ? window : globalThis);
