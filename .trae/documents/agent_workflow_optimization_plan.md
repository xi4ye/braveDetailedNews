# Agent 工作流优化计划

## 问题分析

基于对 `news_processor.py` 和终端输出的分析，当前 Agent 工作流存在以下问题：

1. **已经修复的问题**：提取正文成功后仍继续调用工具的问题
2. **仍需优化的问题**：
   - System Prompt 可以更加明确地指导 Agent 工作
   - 工具调用顺序可以进一步优化
   - 缺少明确的完成信号机制

## 优化任务清单

### [x] 任务 1：提取正文成功后自动保存并结束（已完成）
- **Priority**: P0
- **Depends On**: None
- **Description**: 
  - 当 `extract_content` 成功且已有验证通过的定位器时，自动保存定位器并立即返回结果
  - 无需等待 Agent 调用 `save_locator`
- **Success Criteria**:
  - 提取正文成功后流程立即结束，不再继续调用工具
- **Test Requirements**:
  - `programmatic` TR-1.1: 验证 `extract_content` 成功后直接返回结果，不超过 20 步
- **Notes**: 已在 `news_processor.py:819-922` 中实现

---

### [ ] 任务 2：优化 System Prompt，明确 Agent 工作流程
- **Priority**: P1
- **Depends On**: None
- **Description**: 
  - 在 System Prompt 中明确 Agent 在什么情况下应该做什么
  - 添加更明确的完成条件指导
  - 优化工具使用建议
- **Success Criteria**:
  - Agent 按照更高效的顺序调用工具
  - 减少不必要的工具调用
- **Test Requirements**:
  - `programmatic` TR-2.1: 验证工具调用步骤减少（平均步数 < 10）
  - `human-judgement` TR-2.2: 检查 System Prompt 逻辑是否清晰
- **Notes**: 重点优化 `news_processor.py:756-812` 中的 System Prompt

---

### [ ] 任务 3：进一步优化工具调用顺序和逻辑
- **Priority**: P1
- **Depends On**: 任务 2
- **Description**: 
  - 优化 Agent 的工具选择逻辑
  - 减少重复的页面获取操作
  - 确保工具调用顺序合理
- **Success Criteria**:
  - 工具调用更加高效
  - 避免重复请求同一页面
- **Test Requirements**:
  - `programmatic` TR-3.1: 统计页面请求次数，避免重复请求
- **Notes**: 可能需要调整 System Prompt 或添加更多智能提示

---

### [ ] 任务 4：添加更明确的完成信号检测
- **Priority**: P2
- **Depends On**: 任务 1
- **Description**: 
  - 在 Agent 返回的文本中检测更多完成信号
  - 不仅仅依赖 JSON 格式的响应
- **Success Criteria**:
  - Agent 说"完成"或类似词语时也能正确结束
- **Test Requirements**:
  - `programmatic` TR-4.1: 验证多种完成信号都能被识别
- **Notes**: 优化 `news_processor.py:894-916` 中的完成检测逻辑

---

## 实施建议

1. **立即实施**：任务 1 已完成，可以直接使用
2. **优先实施**：任务 2 和任务 3，这是提高效率的关键
3. **后续优化**：任务 4 作为锦上添花的优化

## 预期收益

- **减少 API 调用**：减少 30-50% 的工具调用
- **提高速度**：处理速度提升 2-3 倍
- **降低成本**：API 调用成本显著降低
