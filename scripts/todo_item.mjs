// 공장 설정 항목처럼 todo.mjs CLI에 없는 항목 조작(제목 바꾸기, 맨 위로)을 sdlc-kit core의 잠금된 update로 한다.
// Usage: node todo_item.mjs retitle <id> <title> | top <id>
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const kit = process.env.SDLC_KIT || path.join(process.env.HOME, '.agents/skills/sdlc-kit');
const core = await import(pathToFileURL(path.join(kit, 'todo', 'core.mjs')).href);
const [command, id, title] = process.argv.slice(2);
const file = core.todoPath();
if (command === 'retitle' && id && title) {
  await core.update(file, id, item => {
    item.title = title.replace(/[\r\n]+/g, ' ').trim();
    item.events.push({ at: core.stamp(), who: 'factory', what: 'retitled' });
  });
} else if (command === 'top' && id) {
  await core.update(file, id, (item, model) => {
    model.items.splice(model.items.indexOf(item), 1);
    model.items.unshift(item);
  });
} else {
  console.error('usage: todo_item.mjs retitle <id> <title> | top <id>');
  process.exit(2);
}
