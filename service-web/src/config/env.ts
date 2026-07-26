import { z } from "zod";

const environmentSchema = z.object({
  VITE_API_BASE_URL: z.url().optional(),
});

export const environment = environmentSchema.parse({
  VITE_API_BASE_URL: import.meta.env.VITE_API_BASE_URL,
});
