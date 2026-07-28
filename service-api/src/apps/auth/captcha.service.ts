import { HttpStatus, Inject, Injectable } from '@nestjs/common';
import { createHash, createHmac, randomInt, randomUUID } from 'node:crypto';
import { deflateSync } from 'node:zlib';

import { AppConfigService } from '../../config/app-config.service.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { RedisService } from '../../shared/redis/redis.service.js';
import { SecurityRateLimitService } from './security-rate-limit.service.js';

const CAPTCHA_ALPHABET = '23456789';
const CAPTCHA_CODE_LENGTH = 4;
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

export const CAPTCHA_CODE_GENERATOR = Symbol('CAPTCHA_CODE_GENERATOR');

/** Generate a short visual CAPTCHA code without retaining it beyond challenge issuance. */
export interface CaptchaCodeGenerator {
  generate(): string;
}

/** Represent only browser context used to bind a challenge without storing raw client data. */
export type CaptchaClientContext = {
  ip: string;
  userAgent: string;
};

/** Expose only the challenge data permitted to cross the API boundary. */
export type CaptchaChallenge = {
  challengeId: string;
  imageDataUrl: string;
  expiresAt: string;
};

@Injectable()
export class SecureCaptchaCodeGenerator implements CaptchaCodeGenerator {
  /** Generate four unambiguous decimal glyphs with cryptographic random selection. */
  public generate(): string {
    let answer = '';
    for (let index = 0; index < CAPTCHA_CODE_LENGTH; index += 1) {
      answer += CAPTCHA_ALPHABET[randomInt(CAPTCHA_ALPHABET.length)] ?? '2';
    }
    return answer;
  }
}

@Injectable()
export class CaptchaService {
  /** Coordinate challenge rendering, opaque Redis storage, and one-time verification. */
  public constructor(
    private readonly redis: RedisService,
    private readonly rateLimit: SecurityRateLimitService,
    private readonly config: AppConfigService,
    @Inject(CAPTCHA_CODE_GENERATOR) private readonly codeGenerator: CaptchaCodeGenerator,
  ) {}

  /** Issue a short-lived PNG challenge after applying an IP-only issuance limit. */
  public async createChallenge(context: CaptchaClientContext): Promise<CaptchaChallenge> {
    await this.rateLimit.assertCaptchaIssueAllowed(context.ip);
    const challengeId = randomUUID();
    const answer = this.codeGenerator.generate();
    const expiresAt = new Date(Date.now() + this.config.captchaTtlSeconds * 1_000);

    try {
      await this.redis.set(
        this.challengeKey(challengeId),
        this.answerDigest(challengeId, context, answer),
        this.config.captchaTtlSeconds,
      );
    } catch {
      // CAPTCHA state is an authentication control, so Redis failures must fail closed.
      throw dependencyUnavailable();
    }

    return {
      challengeId,
      imageDataUrl: renderCaptchaPng(answer),
      expiresAt: expiresAt.toISOString(),
    };
  }

  /** Atomically compare and consume a challenge, including wrong answers and context mismatches. */
  public async verifyAndConsume(
    challengeId: string,
    answer: string,
    context: CaptchaClientContext,
  ): Promise<boolean> {
    try {
      return await this.redis.consumeMatchingValue(
        this.challengeKey(challengeId),
        this.answerDigest(challengeId, context, answer.trim()),
      );
    } catch {
      // Do not continue credential validation when CAPTCHA enforcement cannot be verified.
      throw dependencyUnavailable();
    }
  }

  /** Create a namespaced non-sensitive Redis key from an opaque random challenge ID. */
  private challengeKey(challengeId: string): string {
    return `auth:captcha:${challengeId}`;
  }

  /** Derive a stored HMAC from answer and client context without persisting either plaintext value. */
  private answerDigest(challengeId: string, context: CaptchaClientContext, answer: string): string {
    const binding = createHash('sha256')
      .update(`${context.ip}\u0000${context.userAgent}`)
      .digest('base64url');
    return createHmac('sha256', this.config.captchaHmacSecret)
      .update(`${challengeId}\u0000${binding}\u0000${answer}`)
      .digest('base64url');
  }
}

/** Build stable dependency error without leaking Redis topology or client exception text. */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Authentication security controls unavailable',
  );
}

/** Render an inert, locally encoded PNG so the browser never learns an answer outside image pixels. */
function renderCaptchaPng(answer: string): string {
  const width = 160;
  const height = 56;
  const pixels = Buffer.alloc(width * height * 4, 255);

  // Draw subtle deterministic noise to reduce direct glyph-template matching without client code.
  for (let offset = 0; offset < width * height; offset += 17) {
    setPixel(pixels, width, offset % width, Math.floor(offset / width), 238, 241, 248, 255);
  }
  for (let index = 0; index < answer.length; index += 1) {
    drawGlyph(pixels, width, answer[index] ?? '2', 18 + index * 34, 11, 4);
  }

  return `data:image/png;base64,${encodePng(width, height, pixels).toString('base64')}`;
}

/** Paint one 5×7 numeric glyph at an integer scale using dark indigo pixels. */
function drawGlyph(
  pixels: Buffer,
  width: number,
  character: string,
  originX: number,
  originY: number,
  scale: number,
): void {
  const glyph = DIGITS[character] ?? [
    '01110',
    '10001',
    '00001',
    '00010',
    '00100',
    '01000',
    '11111',
  ];
  for (let row = 0; row < glyph.length; row += 1) {
    const line = glyph[row] ?? '00000';
    for (let column = 0; column < line.length; column += 1) {
      if (line[column] !== '1') {
        continue;
      }
      for (let y = 0; y < scale; y += 1) {
        for (let x = 0; x < scale; x += 1) {
          setPixel(
            pixels,
            width,
            originX + column * scale + x,
            originY + row * scale + y,
            73,
            73,
            156,
            255,
          );
        }
      }
    }
  }
}

/** Write a pixel only when it remains inside the fixed CAPTCHA canvas. */
function setPixel(
  pixels: Buffer,
  width: number,
  x: number,
  y: number,
  red: number,
  green: number,
  blue: number,
  alpha: number,
): void {
  const height = pixels.length / (width * 4);
  if (x < 0 || x >= width || y < 0 || y >= height) {
    return;
  }
  const offset = (y * width + x) * 4;
  pixels[offset] = red;
  pixels[offset + 1] = green;
  pixels[offset + 2] = blue;
  pixels[offset + 3] = alpha;
}

/** Encode RGBA scanlines into a standards-compliant non-interlaced PNG buffer. */
function encodePng(width: number, height: number, pixels: Buffer): Buffer {
  const raw = Buffer.alloc(height * (1 + width * 4));
  for (let row = 0; row < height; row += 1) {
    const rawOffset = row * (1 + width * 4);
    raw[rawOffset] = 0;
    pixels.copy(raw, rawOffset + 1, row * width * 4, (row + 1) * width * 4);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([
    PNG_SIGNATURE,
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

/** Wrap one PNG payload with its type, length, and CRC-32 integrity value. */
function pngChunk(type: string, data: Buffer): Buffer {
  const typeBuffer = Buffer.from(type, 'ascii');
  const result = Buffer.alloc(12 + data.length);
  result.writeUInt32BE(data.length, 0);
  typeBuffer.copy(result, 4);
  data.copy(result, 8);
  result.writeUInt32BE(crc32(Buffer.concat([typeBuffer, data])), 8 + data.length);
  return result;
}

/** Calculate PNG-required unsigned CRC-32 for a compact in-process image encoder. */
function crc32(data: Buffer): number {
  let crc = 0xffffffff;
  for (const value of data) {
    crc ^= value;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

const DIGITS: Readonly<Record<string, readonly string[]>> = {
  '2': ['01110', '10001', '00001', '00010', '00100', '01000', '11111'],
  '3': ['11110', '00001', '00001', '01110', '00001', '00001', '11110'],
  '4': ['00010', '00110', '01010', '10010', '11111', '00010', '00010'],
  '5': ['11111', '10000', '10000', '11110', '00001', '00001', '11110'],
  '6': ['01110', '10000', '10000', '11110', '10001', '10001', '01110'],
  '7': ['11111', '00001', '00010', '00100', '01000', '01000', '01000'],
  '8': ['01110', '10001', '10001', '01110', '10001', '10001', '01110'],
  '9': ['01110', '10001', '10001', '01111', '00001', '00001', '01110'],
};
