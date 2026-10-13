export type EventRecord = {task_id:string;seq:number;timestamp:number;type:string;payload:any};
export type Task = {id:string;state:string;instruction:string;project_id:string;task_type:string;observation?:any;checks?:Record<string,any>;summary?:string;error?:string;diff_hash?:string;final_screenshot?:string;gui_evidence?:any[]};
let csrf = '';
export function setCsrf(value:string) { csrf=value; }
export async function api<T=any>(path:string, data?:unknown):Promise<T> {
  const response=await fetch(path,{credentials:'same-origin',method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:`Request failed (${response.status})`);
  return result;
}
