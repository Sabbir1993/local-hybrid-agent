import { env } from '../config/env';

export type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'none';

export interface LogEntry {
  id: string;
  timestamp: string;
  epoch: number;
  level: 'debug' | 'info' | 'warn' | 'error';
  tag: string;
  message: string;
  data?: unknown;
}

const LEVEL_WEIGHT: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
  none: 100,
};

type LogListener = (entry: LogEntry) => void;

class ApplicationLogger {
  private buffer: LogEntry[] = [];
  private listeners: Set<LogListener> = new Set();
  private maxBuffer: number = env.logMaxBuffer;
  private currentLevel: LogLevel = env.logLevel;
  private projectName: string = env.projectName;

  constructor() {
    // Expose logs on window for debugging if in browser context
    if (typeof window !== 'undefined') {
      (window as unknown as { __APP_LOGGER__: ApplicationLogger }).__APP_LOGGER__ = this;
    }
  }

  public setLevel(level: LogLevel): void {
    this.currentLevel = level;
  }

  public getLevel(): LogLevel {
    return this.currentLevel;
  }

  public subscribe(listener: LogListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  public getLogs(filter?: { level?: LogLevel; tag?: string }): LogEntry[] {
    let result = [...this.buffer];
    if (filter?.level && filter.level !== 'none') {
      result = result.filter((entry) => entry.level === filter.level);
    }
    if (filter?.tag) {
      const lower = filter.tag.toLowerCase();
      result = result.filter((entry) => entry.tag.toLowerCase().includes(lower));
    }
    return result;
  }

  public clear(): void {
    this.buffer = [];
  }

  private shouldLog(level: 'debug' | 'info' | 'warn' | 'error'): boolean {
    return LEVEL_WEIGHT[level] >= LEVEL_WEIGHT[this.currentLevel];
  }

  private record(level: 'debug' | 'info' | 'warn' | 'error', tag: string, message: string, data?: unknown): LogEntry {
    const now = new Date();
    const entry: LogEntry = {
      id: `${now.getTime()}-${Math.random().toString(36).slice(2, 7)}`,
      timestamp: now.toTimeString().split(' ')[0] + '.' + String(now.getMilliseconds()).padStart(3, '0'),
      epoch: now.getTime(),
      level,
      tag,
      message,
      data,
    };

    this.buffer.push(entry);
    if (this.buffer.length > this.maxBuffer) {
      this.buffer.shift();
    }

    this.listeners.forEach((listener) => {
      try {
        listener(entry);
      } catch (err) {
        console.error('[Logger] Listener error:', err);
      }
    });

    return entry;
  }

  private outputConsole(entry: LogEntry): void {
    if (!this.shouldLog(entry.level)) return;

    const prefix = `[${entry.timestamp}] [${this.projectName}] [${entry.tag}]`;
    const styleTag =
      entry.level === 'error'
        ? 'color: #f85149; font-weight: bold;'
        : entry.level === 'warn'
        ? 'color: #d29922; font-weight: bold;'
        : entry.level === 'info'
        ? 'color: #79c0ff; font-weight: 500;'
        : 'color: #8b949e;';

    const args: unknown[] = [`%c${prefix}%c ${entry.message}`, styleTag, 'color: inherit;'];
    if (entry.data !== undefined) {
      args.push(entry.data);
    }

    switch (entry.level) {
      case 'debug':
        console.debug(...args);
        break;
      case 'info':
        console.info(...args);
        break;
      case 'warn':
        console.warn(...args);
        break;
      case 'error':
        console.error(...args);
        break;
    }
  }

  public debug(tag: string, message: string, data?: unknown): void {
    const entry = this.record('debug', tag, message, data);
    this.outputConsole(entry);
  }

  public info(tag: string, message: string, data?: unknown): void {
    const entry = this.record('info', tag, message, data);
    this.outputConsole(entry);
  }

  public warn(tag: string, message: string, data?: unknown): void {
    const entry = this.record('warn', tag, message, data);
    this.outputConsole(entry);
  }

  public error(tag: string, message: string, data?: unknown): void {
    const entry = this.record('error', tag, message, data);
    this.outputConsole(entry);
  }

  /**
   * Creates a dedicated scoped logger instance for a component or service.
   * Example: const log = logger.createScope('ChatStream');
   *          log.info('Connecting to SSE stream');
   */
  public createScope(tag: string) {
    return {
      debug: (msg: string, data?: unknown) => this.debug(tag, msg, data),
      info: (msg: string, data?: unknown) => this.info(tag, msg, data),
      warn: (msg: string, data?: unknown) => this.warn(tag, msg, data),
      error: (msg: string, data?: unknown) => this.error(tag, msg, data),
    };
  }
}

export const logger = new ApplicationLogger();
