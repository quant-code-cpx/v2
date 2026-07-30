import 'reflect-metadata';

import { plainToInstance } from 'class-transformer';
import { validate } from 'class-validator';
import { describe, expect, it } from 'vitest';

import { ListEquitySectorsQueryDto } from '../dto/list-equity-sectors-query.dto.js';

/** 覆盖证券成分叶查询的 publication 与双时态身份参数边界。 */
describe('ListEquitySectorsQueryDto', () => {
  /** 精确版本、日期身份和带偏移知识时刻可以同时构成稳定读取。 */
  it('accepts an exact publication with independent identity time', async () => {
    const input = plainToInstance(ListEquitySectorsQueryDto, {
      scheme: 'eastmoney.industry',
      dataVersion: '00000000-0000-4000-8000-000000000022',
      identityAsOf: '2019-12-31',
      knownAt: '2026-07-30T08:00:00+08:00',
      limit: 200,
    });

    expect(await validate(input)).toHaveLength(0);
  });

  /** 非 UUID publication、非 date-only 身份或无偏移知识时刻都必须在公开边界失败。 */
  it('rejects malformed publication and identity anchors', async () => {
    const input = plainToInstance(ListEquitySectorsQueryDto, {
      scheme: 'eastmoney.industry',
      dataVersion: 'latest',
      identityAsOf: '2026-07-30T00:00:00Z',
      knownAt: '2026-07-30T08:00:00',
      limit: 200,
    });

    const properties = (await validate(input)).map(
      /** 只比较公开字段名，不依赖校验器的内部错误文案。 */
      (error) => error.property,
    );
    expect(properties).toEqual(expect.arrayContaining(['dataVersion', 'identityAsOf', 'knownAt']));
  });

  /** 缺少状态 publication 或业务身份日期都不能形成可消费的股票中心叶查询。 */
  it('requires both publication and identity date anchors', async () => {
    const missingVersion = plainToInstance(ListEquitySectorsQueryDto, {
      scheme: 'eastmoney.industry',
      identityAsOf: '2026-07-30',
    });
    const missingIdentity = plainToInstance(ListEquitySectorsQueryDto, {
      scheme: 'eastmoney.industry',
      dataVersion: '00000000-0000-4000-8000-000000000022',
    });

    expect(validationProperties(await validate(missingVersion))).toContain('dataVersion');
    expect(validationProperties(await validate(missingIdentity))).toContain('identityAsOf');
  });
});

/** 提取公开校验字段名，避免断言依赖 class-validator 内部文案。 */
function validationProperties(errors: Awaited<ReturnType<typeof validate>>): string[] {
  return errors.map(
    /** 每个校验错误只投影稳定属性名。 */
    (error) => error.property,
  );
}
