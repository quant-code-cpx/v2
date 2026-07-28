import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const serviceApiRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const forbiddenControllerDecorator = /@(Get|Put|Patch|Delete|Head|Options|All)\s*\(/;

/** 递归收集目录中的 Nest Controller 源文件，不跟随目录外依赖。 */
function collectControllerFiles(directory: string): string[] {
  const files: string[] = [];

  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...collectControllerFiles(path));
    } else if (entry.isFile() && entry.name.endsWith('.controller.ts')) {
      files.push(path);
    }
  }

  return files;
}

// 汇集 Controller 与 CORS 两层的 POST-only 约束回归测试。
describe('service-api POST-only HTTP policy', () => {
  // 禁止任何 Controller 声明非 POST 路由装饰器。
  it('allows only POST route decorators in controllers', () => {
    const controllerFiles = collectControllerFiles(join(serviceApiRoot, 'src'));
    expect(controllerFiles.length).toBeGreaterThan(0);

    for (const file of controllerFiles) {
      const source = readFileSync(file, 'utf8');
      expect(source, file).not.toMatch(forbiddenControllerDecorator);
      expect(source, file).toMatch(/@Post\s*\(/);
    }
  });

  // 保证 CORS preflight 只向浏览器公布 POST 应用方法。
  it('advertises only POST through CORS', () => {
    const source = readFileSync(join(serviceApiRoot, 'src/bootstrap/configure-api.ts'), 'utf8');
    expect(source).toContain("methods: ['POST']");
  });
});
