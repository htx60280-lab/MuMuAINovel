/* eslint-disable @typescript-eslint/no-explicit-any */
export interface SSEMessage {
  type: 'progress' | 'chunk' | 'content' | 'result' | 'error' | 'done' | 'start' | 'warning' | 'analysis_started' | 'analysis_queued';
  message?: string;
  progress?: number;
  word_count?: number;
  status?: 'processing' | 'success' | 'error' | 'warning';
  content?: string;
  data?: any;
  error?: string;
  code?: number;
  task_id?: string;
}

export interface SSEClientOptions {
  onProgress?: (message: string, progress: number, status: string, wordCount?: number) => void;
  onChunk?: (content: string) => void;
  onResult?: (data: any) => void;
  onError?: (error: string, code?: number) => void;
  onComplete?: () => void;
  onConnectionError?: (error: Event) => void;
}

export class SSEClient {
  private eventSource: EventSource | null = null;
  private url: string;
  private options: SSEClientOptions;
  private accumulatedContent = '';

  constructor(url: string, options: SSEClientOptions = {}) {
    this.url = url;
    this.options = options;
  }

  connect(): Promise<any> {
    return new Promise((resolve, reject) => {
      try {
        this.eventSource = new EventSource(this.url);

        this.eventSource.onmessage = (event) => {
          try {
            const message: SSEMessage = JSON.parse(event.data);
            this.handleMessage(message, resolve, reject);
          } catch (error) {
            console.error('解析SSE消息失败:', error);
          }
        };

        this.eventSource.onerror = (error) => {
          console.error('SSE连接错误:', error);
          if (this.options.onConnectionError) {
            this.options.onConnectionError(error);
          }
          this.close();
          reject(new Error('SSE连接失败'));
        };
      } catch (error) {
        reject(error);
      }
    });
  }

  private handleMessage(message: SSEMessage, resolve: (value: any) => void, reject: (reason?: any) => void) {
    switch (message.type) {
      case 'progress':
        if (this.options.onProgress && message.progress !== undefined) {
          this.options.onProgress(
            message.message || '',
            message.progress,
            message.status || 'processing',
            message.word_count
          );
        }
        break;

      case 'chunk':
        if (message.content) {
          this.accumulatedContent += message.content;
          if (this.options.onChunk) {
            this.options.onChunk(message.content);
          }
        }
        break;

      case 'result':
        if (this.options.onResult && message.data) {
          this.options.onResult(message.data);
        }
        break;

      case 'error':
        if (this.options.onError) {
          this.options.onError(message.error || '未知错误', message.code);
        }
        this.close();
        reject(new Error(message.error || '未知错误'));
        break;

      case 'done':
        if (this.options.onComplete) {
          this.options.onComplete();
        }
        this.close();
        if (!this.options.onResult && this.accumulatedContent) {
          resolve({ content: this.accumulatedContent });
        } else {
          resolve(true);
        }
        break;
    }
  }

  close() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  getAccumulatedContent(): string {
    return this.accumulatedContent;
  }
}

export class SSEPostClient {
  private url: string;
  private data: any;
  private options: SSEClientOptions;
  private abortController: AbortController | null = null;
  private accumulatedContent = '';
  private resultData: any = null;
  private settled = false;

  constructor(url: string, data: any, options: SSEClientOptions = {}) {
    this.url = url;
    this.data = data;
    this.options = options;
  }

  async connect(): Promise<any> {
    return new Promise((resolve, reject) => {
      void this.connectInternal(resolve, reject);
    });
  }

  private async connectInternal(resolve: (value: any) => void, reject: (reason?: any) => void) {
    try {
      this.abortController = new AbortController();

      const response = await fetch(this.url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(this.data),
        signal: this.abortController.signal,
      });

      if (!response.ok) {
        // 尝试读取后端返回的具体错误信息
        let detail = '';
        try {
          const errorBody = await response.json();
          detail = errorBody.detail || errorBody.message || JSON.stringify(errorBody);
        } catch {
          detail = `HTTP ${response.status}`;
        }
        this.rejectOnce(reject, detail, response.status);
        return;
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) {
        throw new Error('无法获取响应流');
      }

      const parseBlock = async (block: string) => {
        if (block.trim() === '' || block.startsWith(':')) {
          return;
        }

        const dataLines = block
          .split(/\r?\n/)
          .filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).trimStart());

        if (dataLines.length === 0) {
          return;
        }

        const message: SSEMessage = JSON.parse(dataLines.join('\n'));
        await this.handleMessage(message, resolve, reject);
      };

      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });

        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() || '';

        for (const block of blocks) {
          try {
            await parseBlock(block);
          } catch (error) {
            console.error('解析SSE消息失败:', error, block);
          }
        }
      }

      if (buffer.trim() !== '') {
        const remainingBlocks = buffer.split(/\r?\n\r?\n/);
        for (const block of remainingBlocks) {
          try {
            await parseBlock(block);
          } catch (error) {
            console.error('解析SSE尾部消息失败:', error, block);
          }
        }
      }

      if (this.settled) {
        return;
      }

      if (this.resultData) {
        this.resolveOnce(resolve, this.resultData);
        return;
      }

      if (this.accumulatedContent) {
        this.resolveOnce(resolve, { content: this.accumulatedContent });
        return;
      }

      this.rejectOnce(reject, '连接中断，未收到完成信号');
    } catch (error: any) {
      if (error.name === 'AbortError') {
        console.log('请求已取消');
        return;
      }

      if (this.settled) {
        return;
      }

      if (this.resultData) {
        this.resolveOnce(resolve, this.resultData);
        return;
      }

      console.error('SSE POST请求失败:', error);
      this.rejectOnce(reject, error.message || '请求失败');
    }
  }

  private async handleMessage(message: SSEMessage, resolve: (value: any) => void, reject: (reason?: any) => void) {
    switch (message.type) {
      case 'progress':
        if (this.options.onProgress && message.progress !== undefined) {
          this.options.onProgress(
            message.message || '',
            message.progress,
            message.status || 'processing',
            message.word_count
          );
        }
        break;

      case 'chunk':
        if (message.content) {
          this.accumulatedContent += message.content;
          if (this.options.onChunk) {
            this.options.onChunk(message.content);
          }
        }
        break;

      case 'result':
        if (this.options.onResult && message.data) {
          this.options.onResult(message.data);
        }
        this.resultData = message.data;
        break;

      case 'error':
        this.rejectOnce(reject, message.error || '未知错误', message.code);
        break;

      case 'done':
        if (this.resultData) {
          this.resolveOnce(resolve, this.resultData);
        } else if (this.accumulatedContent) {
          this.resolveOnce(resolve, { content: this.accumulatedContent });
        } else {
          this.resolveOnce(resolve, true);
        }
        break;
    }
  }

  private resolveOnce(resolve: (value: any) => void, value: any) {
    if (this.settled) {
      return;
    }
    this.settled = true;
    if (this.options.onComplete) {
      this.options.onComplete();
    }
    resolve(value);
  }

  private rejectOnce(reject: (reason?: any) => void, error: string, code?: number) {
    if (this.settled) {
      return;
    }
    this.settled = true;
    if (this.options.onError) {
      this.options.onError(error, code);
    }
    reject(new Error(error));
  }

  abort() {
    if (this.abortController) {
      this.abortController.abort();
    }
  }

  getAccumulatedContent(): string {
    return this.accumulatedContent;
  }
}

export async function ssePost<T = any>(
  url: string,
  data: any,
  options: SSEClientOptions = {}
): Promise<T> {
  const client = new SSEPostClient(url, data, options);
  try {
    return await client.connect();
  } finally {
    client.abort();
  }
}
