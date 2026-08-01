/**
 * RAG 问答页面（重构版）
 *
 * 核心特性：
 * 1. 左右布局：左侧会话列表 + 右侧聊天区
 * 2. 流式输出：token 逐字显示，实时感知
 * 3. 引用溯源 UI：[1][2] 编号可点击高亮对应卡片，展示标题/章节/相关度
 * 4. 工具调用可视化：显示"正在搜索知识库..."等状态
 * 5. 会话管理：切换会话时从后端加载历史消息
 */

import { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Loader2, Globe, BookOpen, Search, Brain, ChevronDown, ThumbsUp, ThumbsDown } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import {
  langgraphChatStream,
  langgraphApi,
  type Citation,
  type LangGraphMessage,
} from '@/services/api';
import { useToastStore } from '@/store/toastStore';
import SessionSidebar from './SessionSidebar';

// ========== 类型定义 ==========

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  toolsUsed?: string[];
  isStreaming?: boolean;
  timestamp: string;
}

// ========== 工具名称映射（用于可视化） ==========

const TOOL_LABELS: Record<string, { label: string; icon: typeof Search }> = {
  search_knowledge: { label: '搜索知识库', icon: BookOpen },
  web_search: { label: '搜索互联网', icon: Globe },
  crawl_webpage: { label: '爬取网页', icon: Search },
  generate_content: { label: '生成内容', icon: Brain },
  rag_search: { label: 'RAG 检索', icon: BookOpen },
  summarize_documents: { label: '总结文档', icon: Brain },
};

// ========== 主组件 ==========

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [useWebSearch, setUseWebSearch] = useState(false);
  const [activeTools, setActiveTools] = useState<string[]>([]);
  const [sidebarTrigger, setSidebarTrigger] = useState(0);
  const [expandedCitations, setExpandedCitations] = useState<Set<string>>(new Set());
  const [loadingHistory, setLoadingHistory] = useState(false);
  // 反馈状态：每条消息的点赞/点踩
  const [feedbackState, setFeedbackState] = useState<Record<string, 'positive' | 'negative' | undefined>>({});
  // 点踩时的备注输入
  const [negativeComment, setNegativeComment] = useState<Record<string, string>>({});
  // 反馈提交中状态
  const [submittingFeedback, setSubmittingFeedback] = useState<Record<string, boolean>>({});

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);

  const toast = useToastStore();

  // 滚动到底部
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // ========== 会话管理 ==========

  // 新建会话
  const handleNewSession = useCallback(() => {
    setMessages([]);
    setSessionId(null);
  }, []);

  // 切换会话：从后端加载历史消息
  const handleSessionSelect = useCallback(async (sid: string) => {
    setLoadingHistory(true);
    setSessionId(sid);
    try {
      const data = await langgraphApi.getSessionMessages(sid, 100);
      const historyMessages: ChatMessage[] = (data.messages || []).map((msg: LangGraphMessage, idx: number) => ({
        id: `${sid}_${idx}`,
        role: msg.role === 'user' ? 'user' : 'assistant',
        content: msg.content,
        toolsUsed: msg.tools_used || undefined,
        timestamp: msg.timestamp || new Date().toISOString(),
      }));
      setMessages(historyMessages);
    } catch (error) {
      console.error('加载会话历史失败:', error);
      setMessages([]);
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  // ========== 发送消息（流式） ==========

  const handleSend = async () => {
    if (!input.trim() || loading) return;

    const userContent = input.trim();
    const userMessage: ChatMessage = {
      id: `user_${Date.now()}`,
      role: 'user',
      content: userContent,
      timestamp: new Date().toISOString(),
    };

    // 创建占位的 assistant 消息（用于流式显示）
    const assistantId = `assistant_${Date.now()}`;
    const assistantPlaceholder: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      isStreaming: true,
      timestamp: new Date().toISOString(),
    };

    setMessages(prev => [...prev, userMessage, assistantPlaceholder]);
    setInput('');
    setLoading(true);
    setActiveTools([]);

    try {
      await langgraphChatStream(userContent, sessionId || undefined, useWebSearch, {
        onStart: (sid) => {
          // 更新 session_id（首次对话时后端自动创建）
          if (sid && sid !== sessionId) {
            setSessionId(sid);
          }
        },
        onToken: (token) => {
          // 逐 token 追加到 assistant 消息
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, content: m.content + token }
                : m
            )
          );
        },
        onToolCalls: (tools) => {
          setActiveTools(tools);
        },
        onToolResult: () => {
          // 工具结果到达后清除工具状态（可选：展示结果摘要）
          setActiveTools([]);
        },
        onDone: (data) => {
          // 流结束：填充 citations 和 toolsUsed，移除 isStreaming 标记
          // 如果后端检测到敏感信息，用脱敏内容替换流式累积的内容
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? {
                    ...m,
                    isStreaming: false,
                    content: data.sanitized_content || m.content,
                    citations: data.citations.length > 0 ? data.citations : undefined,
                    toolsUsed: data.tools_used.length > 0 ? data.tools_used : undefined,
                  }
                : m
            )
          );
          // 更新 session_id
          if (data.session_id && data.session_id !== sessionId) {
            setSessionId(data.session_id);
          }
          // 触发侧边栏刷新（标题可能已自动生成）
          setSidebarTrigger(prev => prev + 1);
          setActiveTools([]);
        },
        onError: (message) => {
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, isStreaming: false, content: m.content || `错误：${message}` }
                : m
            )
          );
          setActiveTools([]);
        },
      });
    } catch (error) {
      console.error('流式请求失败:', error);
      setMessages(prev =>
        prev.map(m =>
          m.id === assistantId
            ? { ...m, isStreaming: false, content: m.content || '抱歉，发生了网络错误，请稍后重试。' }
            : m
        )
      );
    } finally {
      setLoading(false);
    }
  };

  // ========== 引用溯源交互 ==========

  const toggleCitationExpansion = (messageId: string) => {
    setExpandedCitations(prev => {
      const next = new Set(prev);
      if (next.has(messageId)) {
        next.delete(messageId);
      } else {
        next.add(messageId);
      }
      return next;
    });
  };

  // ========== 答案反馈 ==========

  // 点赞：直接提交
  const handlePositiveFeedback = async (message: ChatMessage) => {
    if (!sessionId || feedbackState[message.id] || submittingFeedback[message.id]) return;
    setSubmittingFeedback(prev => ({ ...prev, [message.id]: true }));
    try {
      await langgraphApi.submitFeedback({
        session_id: sessionId,
        message_content: message.content,
        rating: 'positive',
      });
      setFeedbackState(prev => ({ ...prev, [message.id]: 'positive' }));
      toast.success('感谢反馈');
    } catch (error) {
      console.error('提交反馈失败:', error);
      toast.error('反馈提交失败，请稍后重试');
    } finally {
      setSubmittingFeedback(prev => ({ ...prev, [message.id]: false }));
    }
  };

  // 点踩：展开输入框（不立即提交）
  const handleNegativeClick = (messageId: string) => {
    if (feedbackState[messageId] || submittingFeedback[messageId]) return;
    setFeedbackState(prev => ({ ...prev, [messageId]: 'negative' }));
  };

  // 提交点踩反馈（带备注）
  const handleNegativeSubmit = async (message: ChatMessage) => {
    if (!sessionId || submittingFeedback[message.id]) return;
    const comment = negativeComment[message.id]?.trim();
    setSubmittingFeedback(prev => ({ ...prev, [message.id]: true }));
    try {
      await langgraphApi.submitFeedback({
        session_id: sessionId,
        message_content: message.content,
        rating: 'negative',
        comment: comment || undefined,
      });
      toast.success('感谢反馈');
      // 提交后清空输入框
      setNegativeComment(prev => {
        const next = { ...prev };
        delete next[message.id];
        return next;
      });
    } catch (error) {
      console.error('提交反馈失败:', error);
      toast.error('反馈提交失败，请稍后重试');
    } finally {
      setSubmittingFeedback(prev => ({ ...prev, [message.id]: false }));
    }
  };

  // 按 Enter 发送
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // ========== 渲染 ==========

  return (
    <div className="h-full flex">
      {/* 左侧：会话列表 */}
      <aside className="w-60 flex-shrink-0 border-r border-zinc-200 bg-white p-3 overflow-hidden">
        <SessionSidebar
          currentSessionId={sessionId}
          onSessionSelect={handleSessionSelect}
          onNewSession={handleNewSession}
          refreshTrigger={sidebarTrigger}
        />
      </aside>

      {/* 右侧：聊天区 */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* 消息列表 */}
        <div ref={messagesContainerRef} className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-6 py-6">
            {loadingHistory ? (
              <div className="flex items-center justify-center h-full text-zinc-400 py-20">
                <Loader2 className="animate-spin mr-2" size={18} />
                <span className="text-sm">加载历史消息</span>
              </div>
            ) : messages.length === 0 ? (
              <div className="flex items-center justify-center h-full text-zinc-400 py-20">
                <div className="text-center">
                  <p className="text-base text-zinc-700 font-medium mb-1">开始提问</p>
                  <p className="text-[13px]">基于知识库回答问题，支持多轮对话和引用溯源</p>
                </div>
              </div>
            ) : (
              <div className="space-y-5">
                {messages.map(message => (
                  <div key={message.id} className="animate-fade-in">
                    {/* 用户消息 */}
                    {message.role === 'user' && (
                      <div className="flex justify-end">
                        <div className="message-user max-w-[75%]">
                          <p className="whitespace-pre-wrap">{message.content}</p>
                        </div>
                      </div>
                    )}

                    {/* 助手消息 */}
                    {message.role === 'assistant' && (
                      <div className="flex justify-start">
                        <div className="max-w-[85%] w-full">
                          <div className="markdown-content">
                            {message.content ? (
                              <ReactMarkdown>{message.content}</ReactMarkdown>
                            ) : message.isStreaming ? (
                              <div className="typing-indicator">
                                <span></span>
                                <span></span>
                                <span></span>
                              </div>
                            ) : (
                              <span className="text-zinc-400">（无内容）</span>
                            )}
                            {/* 流式光标 */}
                            {message.isStreaming && message.content && (
                              <span className="inline-block w-1.5 h-4 bg-zinc-700 animate-pulse ml-0.5 align-text-bottom" />
                            )}
                          </div>

                          {/* 工具调用标签 */}
                          {message.toolsUsed && message.toolsUsed.length > 0 && (
                            <div className="mt-2.5 flex flex-wrap gap-1">
                              {message.toolsUsed.map((tool, idx) => {
                                const info = TOOL_LABELS[tool];
                                const Icon = info?.icon || Search;
                                return (
                                  <span key={idx} className="tag-default">
                                    <Icon size={11} className="mr-1" />
                                    {info?.label || tool}
                                  </span>
                                );
                              })}
                            </div>
                          )}

                          {/* 引用溯源 */}
                          {message.citations && message.citations.length > 0 && (
                            <div className="mt-3 pt-3 border-t border-zinc-100">
                              <button
                                onClick={() => toggleCitationExpansion(message.id)}
                                className="flex items-center gap-1 text-[12px] text-zinc-500 hover:text-zinc-900 transition-colors"
                              >
                                <ChevronDown
                                  size={13}
                                  className={`transition-transform ${
                                    expandedCitations.has(message.id) ? 'rotate-180' : ''
                                  }`}
                                />
                                <span>引用来源（{message.citations.length}）</span>
                              </button>

                              {expandedCitations.has(message.id) && (
                                <div className="mt-2 space-y-1.5">
                                  {message.citations.map((citation) => (
                                    <div
                                      key={citation.index}
                                      id={`citation-${message.id}-${citation.index}`}
                                      className="p-2.5 bg-zinc-50 rounded-lg border border-zinc-200 hover:border-zinc-300 transition-colors"
                                    >
                                      <div className="flex items-start justify-between gap-2">
                                        <div className="flex-1 min-w-0">
                                          <div className="flex items-center gap-1.5">
                                            <span className="text-[11px] font-medium text-zinc-400">
                                              [{citation.index}]
                                            </span>
                                            <span className="text-[13px] text-zinc-700 truncate">
                                              {citation.title || '未知文档'}
                                            </span>
                                          </div>
                                          {citation.heading_path && (
                                            <div className="text-[11px] text-zinc-400 mt-0.5 truncate">
                                              {citation.heading_path}
                                            </div>
                                          )}
                                        </div>
                                        {/* 相关度分数 */}
                                        <div className="flex-shrink-0 flex items-center gap-1.5">
                                          <div className="w-10 h-1 bg-zinc-200 rounded-full overflow-hidden">
                                            <div
                                              className="h-full bg-zinc-700 rounded-full"
                                              style={{ width: `${Math.round(citation.score * 100)}%` }}
                                            />
                                          </div>
                                          <span className="text-[11px] text-zinc-400 tabular-nums">
                                            {citation.score.toFixed(2)}
                                          </span>
                                        </div>
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                          )}

                          {/* 答案反馈：仅在非流式状态下显示 */}
                          {message.isStreaming !== true && (
                            <div className="mt-2.5 pt-2.5 border-t border-zinc-100">
                              <div className="flex items-center gap-1">
                                <button
                                  onClick={() => handlePositiveFeedback(message)}
                                  disabled={!!feedbackState[message.id] || submittingFeedback[message.id]}
                                  aria-label="点赞"
                                  title="有帮助"
                                  className={`p-1 rounded transition-colors disabled:cursor-default ${
                                    feedbackState[message.id] === 'positive'
                                      ? 'text-zinc-900'
                                      : 'text-zinc-400 hover:text-zinc-900'
                                  }`}
                                >
                                  {submittingFeedback[message.id] && feedbackState[message.id] === undefined ? (
                                    <Loader2 className="animate-spin" size={14} />
                                  ) : (
                                    <ThumbsUp size={14} />
                                  )}
                                </button>
                                <button
                                  onClick={() => handleNegativeClick(message.id)}
                                  disabled={!!feedbackState[message.id] || submittingFeedback[message.id]}
                                  aria-label="点踩"
                                  title="需要改进"
                                  className={`p-1 rounded transition-colors disabled:cursor-default ${
                                    feedbackState[message.id] === 'negative'
                                      ? 'text-zinc-900'
                                      : 'text-zinc-400 hover:text-zinc-900'
                                  }`}
                                >
                                  <ThumbsDown size={14} />
                                </button>
                              </div>

                              {/* 点踩后展开的备注输入框 */}
                              {feedbackState[message.id] === 'negative' && (
                                <div className="mt-2 flex gap-2">
                                  <input
                                    type="text"
                                    value={negativeComment[message.id] || ''}
                                    onChange={e =>
                                      setNegativeComment(prev => ({
                                        ...prev,
                                        [message.id]: e.target.value,
                                      }))
                                    }
                                    placeholder="请告诉我们哪里可以改进（可选）"
                                    className="input flex-1 text-[13px]"
                                    disabled={submittingFeedback[message.id]}
                                  />
                                  <button
                                    onClick={() => handleNegativeSubmit(message)}
                                    disabled={submittingFeedback[message.id]}
                                    className="btn-primary px-3 text-[13px]"
                                  >
                                    {submittingFeedback[message.id] ? (
                                      <Loader2 className="animate-spin" size={14} />
                                    ) : (
                                      '提交'
                                    )}
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* 工具调用中状态 */}
            {activeTools.length > 0 && (
              <div className="flex justify-start mt-3">
                <div className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-zinc-50 border border-zinc-200 rounded-lg text-[13px] text-zinc-600">
                  <Loader2 className="animate-spin" size={14} />
                  {activeTools.map(tool => {
                    const info = TOOL_LABELS[tool];
                    return info ? info.label : tool;
                  }).join('、')}
                  <span className="text-zinc-400">中</span>
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* 输入区 */}
        <div className="border-t border-zinc-200 bg-white">
          <div className="max-w-3xl mx-auto px-6 py-3">
            {/* 选项栏 */}
            <div className="flex items-center mb-2">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={useWebSearch}
                  onChange={e => setUseWebSearch(e.target.checked)}
                  className="rounded border-zinc-300 text-zinc-900 focus:ring-zinc-400"
                />
                <span className="text-[13px] text-zinc-600">联网搜索</span>
              </label>
              <span className="text-[12px] text-zinc-400 ml-2">
                {useWebSearch ? '知识库无结果时搜索互联网' : '仅搜索知识库'}
              </span>
            </div>
            {/* 输入框和发送按钮 */}
            <div className="flex gap-2">
              <textarea
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="输入问题..."
                aria-label="问题输入框"
                className="input flex-1"
                rows={1}
                disabled={loading}
              />
              <button
                onClick={handleSend}
                disabled={!input.trim() || loading}
                aria-label="发送消息"
                className="btn-primary px-3.5"
              >
                {loading ? <Loader2 className="animate-spin" size={18} /> : <Send size={18} />}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
