/** DSH 0.1.5 headless accepts argv only; replace its startup provider for large prompts. */
export const name = "preloop-headless-startup";
export async function apply(ctx) {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const task = Buffer.concat(chunks).toString("utf8");
  if (!task.trim()) throw new Error("Preloop task is empty");
  ctx.provide("headlessStartup", { task });
}
